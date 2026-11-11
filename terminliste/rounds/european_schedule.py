"""Resolves each Norwegian entrant's UEFA path to outcome-tree commitments
(#29, #32, #8).

A cup round's opponent is unknown but its place in the competition is taken as
given (see `rounds/cup_schedule.py`). A European qualifying round is the
opposite: the opponent is often drawn well ahead, but whether the team is
still in *this* round rather than having gone out or dropped into another
competition is exactly what isn't known.

Each round is two legs, each with its own `forced_date` or window, which
`resolve_all_legs` turns into one real date apiece. A commitment is therefore a
handful of *specific* dates, not a blocked span — that is what lets a normal
European week (leg, league match, leg) work.

Every team gets an outcome tree (`rounds/outcome_tree.py`): a node per round it
may play, with a win and a loss edge, where a loss either drops into another
competition's round (`drop_to_*`), goes to a main tournament (`on_loss_main`)
or ends the run. Each node's `reached_probability` follows from the ties'
`win_probability`, and only commitments at or above the season's
`contingency_policy.hard_threshold` block the league calendar. Once a real
result is known, mark the tie `reached: true` (or delete the branch not taken)
and regenerate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..model.schema import (
    WEEKDAYS,
    Competition,
    EuropeanLeg,
    EuropeanMatchday,
    EuropeanRound,
    EuropeanTie,
    Season,
    Weekday,
)
from .commitments import Commitment, CommitmentSet
from .outcome_tree import OutcomeNode, OutcomeTree


class EuropeanCascadeError(Exception):
    """A team's European cascade cannot be resolved — a data problem (an
    entrant missing from every round of its own competition, or a
    `drop_to_round` with no matching round in the target competition), not
    something a solver retry can fix."""


ResolvedLegs = dict[tuple[str, str], tuple[date, date]]


def resolve_leg_date(
    leg: EuropeanLeg | EuropeanMatchday,
    blackouts: set[date],
    matchday_weekdays: Sequence[Weekday] = (),
) -> tuple[date, bool]:
    """One leg's window (or forced date) resolved to an actual date.

    A `forced_date` is used exactly; the second return value flags one that
    happens to be blacked out, which is a data conflict worth a warning rather
    than something to paper over.

    A window resolves to the competition's matchday (#112): the first
    non-blackout day on the most preferred of `matchday_weekdays` it contains,
    since UEFA windows span several nights but each competition plays on its
    own. With no matching day it falls back to the earliest non-blackout day,
    then to `window_start` (flagged) if every day is blacked out. A confirmed
    date is confirmed whatever weekday it falls on, so a `forced_date` ignores
    the weekdays.
    """
    if leg.is_forced:
        anchor = leg.forced_date
        return anchor, anchor in blackouts

    span = (leg.window_end - leg.window_start).days
    for weekday in matchday_weekdays:
        target = WEEKDAYS.index(weekday)
        for offset in range(span + 1):
            candidate = leg.window_start + timedelta(days=offset)
            if candidate.weekday() == target and candidate not in blackouts:
                return candidate, False

    for offset in range(span + 1):
        candidate = leg.window_start + timedelta(days=offset)
        if candidate not in blackouts:
            return candidate, False
    return leg.window_start, True


def resolve_all_legs(
    competitions: list[Competition], blackouts: set[date]
) -> tuple[ResolvedLegs, list[str]]:
    """Resolve every round's two legs, across every competition given.

    No round-to-round ordering or rest-gap check, unlike a cup: each round's
    dates come from real UEFA scheduling data rather than being inferred from
    where the previous round landed.
    """
    resolved: ResolvedLegs = {}
    warnings: list[str] = []
    for competition in competitions:
        for round_ in competition.european_rounds:
            first_date, first_blacked_out = resolve_leg_date(
                round_.first_leg, blackouts, competition.matchday_weekdays
            )
            second_date, second_blacked_out = resolve_leg_date(
                round_.second_leg, blackouts, competition.matchday_weekdays
            )
            resolved[(competition.id, round_.id)] = (first_date, second_date)
            if first_blacked_out:
                warnings.append(
                    f"{competition.name}: {round_.name} first leg has no legal (non-blackout) "
                    f"date in its window — placed on {first_date} anyway"
                )
            if second_blacked_out:
                warnings.append(
                    f"{competition.name}: {round_.name} second leg has no legal (non-blackout) "
                    f"date in its window — placed on {second_date} anyway"
                )
    return resolved, warnings


@dataclass(frozen=True)
class MainStage:
    """One stage of a main tournament a team may play: the league phase (all
    its matchdays) or one knockout round."""

    id: str
    name: str
    legs: tuple[tuple[str, date], ...]
    advance_probability: float


def resolve_team_tree(
    team_id: str,
    home_competition: Competition,
    competitions_by_id: dict[str, Competition],
    resolved_legs: ResolvedLegs,
    main_stages: dict[str, list[MainStage]] | None = None,
    tree: OutcomeTree | None = None,
) -> OutcomeTree:
    """`team_id`'s outcome tree, starting from its entry round in
    `home_competition`.

    A win continues to the team's next round of the same competition, or into
    a main tournament when the round sets `on_win_main`. A loss drops to
    another competition's round (`drop_to_*`), goes to a main tournament
    (`on_loss_main`), or ends the run.
    """
    entry = _entry_round(team_id, home_competition)
    if entry is None:
        raise EuropeanCascadeError(
            f"{team_id!r} is not an entrant of any round in {home_competition.id!r}"
        )
    tree = tree or OutcomeTree(team_id)
    stages_cache = main_stages if main_stages is not None else {}
    in_progress: set[str] = set()

    def _main(main_id: str) -> Competition:
        main = competitions_by_id.get(main_id)
        if main is None:
            raise EuropeanCascadeError(f"{team_id!r} is routed into unknown competition {main_id!r}")
        return main

    def _stages(main: Competition) -> list[MainStage]:
        if main.id not in stages_cache:
            stages_cache[main.id] = resolve_main_stages(main, set())[0]
        return stages_cache[main.id]

    def add_main_stages(main: Competition) -> None:
        stages = _stages(main)
        for position, stage in enumerate(stages):
            node_id = f"{team_id}/{main.id}/{stage.id}"
            if node_id in tree.nodes:
                return
            following = stages[position + 1] if position + 1 < len(stages) else None
            tree.add(
                OutcomeNode(
                    node_id=node_id,
                    team_id=team_id,
                    competition_id=main.id,
                    round_id=stage.id,
                    source="uefa",
                    min_rest_days=main.min_rest_days,
                    legs=stage.legs,
                    on_win=f"{team_id}/{main.id}/{following.id}" if following else None,
                    win_probability=stage.advance_probability,
                )
            )

    def round_node(competition: Competition, round_: EuropeanRound) -> str:
        node_id = f"{team_id}/{competition.id}/{round_.id}"
        if node_id in tree.nodes:
            return node_id
        if node_id in in_progress:
            raise EuropeanCascadeError(f"{team_id!r}: cascade loops back to {node_id!r}")
        in_progress.add(node_id)

        # Targets are resolved first so the node can be added before its
        # successors, which keeps `tree.nodes` in walk order (entry first).
        win_target: tuple[str, Competition, EuropeanRound | None] | None = None
        if round_.on_win_main is not None:
            win_target = ("main", _main(round_.on_win_main), None)
        else:
            win_next = _next_round_for_team(team_id, competition, round_)
            if win_next is not None:
                win_target = ("round", competition, win_next)

        loss_target: tuple[str, Competition, EuropeanRound | None] | None = None
        if round_.on_loss_main is not None:
            loss_target = ("main", _main(round_.on_loss_main), None)
        elif round_.drop_to_competition is not None:
            drop_competition = competitions_by_id.get(round_.drop_to_competition)
            if drop_competition is None:
                raise EuropeanCascadeError(
                    f"{competition.id!r} round {round_.id!r} drops into unknown "
                    f"competition {round_.drop_to_competition!r}"
                )
            drop_round = _round_by_id(drop_competition, round_.drop_to_round)
            if drop_round is None:
                raise EuropeanCascadeError(
                    f"{competition.id!r} round {round_.id!r} drops into "
                    f"{round_.drop_to_competition!r} round {round_.drop_to_round!r}, "
                    f"which does not exist there"
                )
            loss_target = ("round", drop_competition, drop_round)

        def target_id(target: tuple[str, Competition, EuropeanRound | None] | None) -> str | None:
            if target is None:
                return None
            kind, target_competition, target_round = target
            if kind == "main":
                stages = _stages(target_competition)
                return f"{team_id}/{target_competition.id}/{stages[0].id}" if stages else None
            return f"{team_id}/{target_competition.id}/{target_round.id}"

        tie = next((t for t in round_.ties if t.team == team_id), None)
        first_date, second_date = resolved_legs[(competition.id, round_.id)]
        tree.add(
            OutcomeNode(
                node_id=node_id,
                team_id=team_id,
                competition_id=competition.id,
                round_id=round_.id,
                source="uefa",
                min_rest_days=competition.min_rest_days,
                legs=(
                    (f"{competition.name}: {round_.name} (first leg)", first_date),
                    (f"{competition.name}: {round_.name} (second leg)", second_date),
                ),
                on_win=target_id(win_target),
                on_loss=target_id(loss_target),
                win_probability=tie.win_probability if tie else 0.5,
                known_reached=tie.reached if tie else False,
            )
        )
        for target in (win_target, loss_target):
            if target is None:
                continue
            kind, target_competition, target_round = target
            if kind == "main":
                add_main_stages(target_competition)
            else:
                round_node(target_competition, target_round)
        in_progress.discard(node_id)
        return node_id

    tree.roots.append(round_node(home_competition, entry))
    return tree


def resolve_team_cascade(
    team_id: str,
    home_competition: Competition,
    competitions_by_id: dict[str, Competition],
    resolved_legs: ResolvedLegs,
    main_stages: dict[str, list[MainStage]] | None = None,
    hard_threshold: float = 1.0,
) -> list[Commitment]:
    """Every commitment on `team_id`'s tree, hard where the team reaches it
    with at least `hard_threshold` probability."""
    tree = resolve_team_tree(
        team_id, home_competition, competitions_by_id, resolved_legs, main_stages
    )
    return tree.finalize().commitments(hard_threshold)


def _entry_round(team_id: str, competition: Competition) -> EuropeanRound | None:
    for round_ in competition.european_rounds:
        if team_id in round_.entrants:
            return round_
    return None


def _round_by_id(competition: Competition, round_id: str | None) -> EuropeanRound | None:
    return next((r for r in competition.european_rounds if r.id == round_id), None)


def _next_round_for_team(
    team_id: str, competition: Competition, round_: EuropeanRound
) -> EuropeanRound | None:
    """The next round after `round_` that `team_id` actually plays.

    Scans forward rather than checking only the next list entry: a
    competition's rounds interleave several UEFA paths (Champions Path vs.
    League Path), so the immediately following round may belong to one this
    team isn't on.
    """
    rounds = competition.european_rounds
    index = next(i for i, r in enumerate(rounds) if r.id == round_.id)
    for following in rounds[index + 1 :]:
        if team_id in following.entrants:
            return following
    return None


def resolve_main_stages(
    competition: Competition, blackouts: set[date]
) -> tuple[list[MainStage], list[str]]:
    """A main tournament as stages a team may play (#79, #8): the league phase
    with every matchday date, then each knockout round with its leg dates,
    each with a display label and the chance of getting through it."""
    warnings: list[str] = []
    league_legs: list[tuple[str, date]] = []

    for matchday in competition.league_phase_matchdays:
        resolved, blacked_out = resolve_leg_date(matchday, blackouts, competition.matchday_weekdays)
        league_legs.append((f"{competition.name}: {matchday.name}", resolved))
        if blacked_out:
            warnings.append(
                f"{competition.name}: {matchday.name} has no legal (non-blackout) date in its "
                f"window — placed on {resolved} anyway"
            )

    stages: list[MainStage] = []
    if league_legs:
        # 0.5 is a flat estimate: top-8 teams skip the play-off, so later knockout
        # rounds come out about 2x too low.
        stages.append(
            MainStage("league", "League phase", tuple(league_legs), competition.advance_probability)
        )

    for round_ in competition.knockout_rounds:
        first_date, first_blacked_out = resolve_leg_date(
            round_.first_leg, blackouts, competition.matchday_weekdays
        )
        first_label = (
            f"{competition.name}: {round_.name}"
            if round_.second_leg is None
            else f"{competition.name}: {round_.name} (first leg)"
        )
        legs = [(first_label, first_date)]
        if first_blacked_out:
            warnings.append(
                f"{competition.name}: {round_.name} has no legal (non-blackout) date in its "
                f"window — placed on {first_date} anyway"
            )
        if round_.second_leg is not None:
            second_date, second_blacked_out = resolve_leg_date(
                round_.second_leg, blackouts, competition.matchday_weekdays
            )
            legs.append((f"{competition.name}: {round_.name} (second leg)", second_date))
            if second_blacked_out:
                warnings.append(
                    f"{competition.name}: {round_.name} second leg has no legal (non-blackout) "
                    f"date in its window — placed on {second_date} anyway"
                )
        advance = (
            round_.advance_probability
            if round_.advance_probability is not None
            else competition.advance_probability
        )
        stages.append(MainStage(round_.id, round_.name, tuple(legs), advance))

    return stages, warnings


def build_main_tournament_rounds_for_display(
    competition: Competition, reachable_teams: Iterable[str], blackouts: set[date]
) -> tuple[list[EuropeanRound], ResolvedLegs]:
    """Adapt a main tournament's matchdays and knockout rounds into the
    `EuropeanRound`/`ResolvedLegs` shape `resolve_all_legs` produces, so the
    report's European views can display one unchanged.

    Pairings aren't modelled, only dates, so every reachable team becomes a
    synthetic tie with the "TBD" opponent and no `home_leg` — the same
    not-yet-confirmed state an undrawn qualifying tie shows. A single-match
    knockout round becomes one whose two legs share a date, and its
    `venue_name` folds into the round's `note`, since the venue helpers are
    shaped around a *team's* ground rather than a neutral one.
    """
    ties = [EuropeanTie(team=team_id) for team_id in sorted(set(reachable_teams))]

    rounds: list[EuropeanRound] = []
    resolved_legs: ResolvedLegs = {}

    for matchday in competition.league_phase_matchdays:
        resolved_date, _ = resolve_leg_date(matchday, blackouts, competition.matchday_weekdays)
        resolved_legs[(competition.id, matchday.id)] = (resolved_date, resolved_date)
        rounds.append(
            EuropeanRound(
                id=matchday.id,
                name=matchday.name,
                first_leg=EuropeanLeg(forced_date=resolved_date),
                second_leg=EuropeanLeg(forced_date=resolved_date),
                ties=ties,
                note=matchday.note,
            )
        )

    for round_ in competition.knockout_rounds:
        first_date, _ = resolve_leg_date(round_.first_leg, blackouts, competition.matchday_weekdays)
        if round_.second_leg is None:
            second_date = first_date
        else:
            second_date, _ = resolve_leg_date(
                round_.second_leg, blackouts, competition.matchday_weekdays
            )
        resolved_legs[(competition.id, round_.id)] = (first_date, second_date)
        note = round_.note
        if round_.venue_name:
            note = f"{note} Venue: {round_.venue_name}." if note else f"Venue: {round_.venue_name}."
        rounds.append(
            EuropeanRound(
                id=round_.id,
                name=round_.name,
                first_leg=EuropeanLeg(forced_date=first_date),
                second_leg=EuropeanLeg(forced_date=second_date),
                ties=ties,
                note=note,
            )
        )

    return rounds, resolved_legs


def resolve_european_trees(
    competitions: list[Competition], season: Season
) -> tuple[dict[str, OutcomeTree], list[str]]:
    """Every entrant's outcome tree across every European competition given,
    qualifying and main tournament alike, with probabilities computed.

    `competitions` should be every European competition in the season, so a
    `drop_to_competition` or `on_*_main` pointer always resolves. Only global
    blackouts are consulted: European qualifiers book no venues.

    Only a team's *true* entry round is walked from — a round that is some
    other round's drop target is reached by that walk, and starting from it
    too would add a second root at the cascade's midpoint. The skip is keyed
    on (team, round), since one round can be one team's direct entry point and
    another's landing spot.
    """
    blackouts = set(season.blacked_out_dates)
    resolved_legs, warnings = resolve_all_legs(competitions, blackouts)

    main_stages: dict[str, list[MainStage]] = {}
    for competition in competitions:
        if competition.is_main_tournament:
            stages, stage_warnings = resolve_main_stages(competition, blackouts)
            main_stages[competition.id] = stages
            warnings.extend(stage_warnings)

    routed_to = {
        (team_id, round_.drop_to_competition, round_.drop_to_round)
        for competition in competitions
        for round_ in competition.european_rounds
        if round_.drop_to_competition is not None
        for team_id in round_.entrants
    }
    by_id = {c.id: c for c in competitions}

    trees: dict[str, OutcomeTree] = {}
    for competition in competitions:
        for team_id in competition.teams:
            entry = _entry_round(team_id, competition)
            if entry is None:
                continue
            if (team_id, competition.id, entry.id) in routed_to:
                continue
            trees[team_id] = resolve_team_tree(
                team_id, competition, by_id, resolved_legs, main_stages, trees.get(team_id)
            )

    for tree in trees.values():
        tree.finalize()
    return trees, warnings


def commitments_from_trees(trees: Iterable[OutcomeTree], hard_threshold: float) -> CommitmentSet:
    return CommitmentSet(c for tree in trees for c in tree.commitments(hard_threshold))


def resolve_european_commitments(
    competitions: list[Competition], season: Season
) -> tuple[CommitmentSet, list[str]]:
    """Every European commitment of every entrant, hard or contingent
    according to the season's `contingency_policy`."""
    trees, warnings = resolve_european_trees(competitions, season)
    return commitments_from_trees(trees.values(), season.contingency_policy.hard_threshold), warnings


def teams_reaching(trees: Iterable[OutcomeTree], competition_id: str) -> list[str]:
    """Teams with any chance of playing a round of `competition_id`."""
    return sorted(
        tree.team_id
        for tree in trees
        if any(node.competition_id == competition_id for node in tree.nodes.values())
    )


__all__ = [
    "EuropeanCascadeError",
    "MainStage",
    "build_main_tournament_rounds_for_display",
    "commitments_from_trees",
    "resolve_all_legs",
    "resolve_european_commitments",
    "resolve_european_trees",
    "resolve_leg_date",
    "resolve_main_stages",
    "resolve_team_cascade",
    "resolve_team_tree",
    "teams_reaching",
]
