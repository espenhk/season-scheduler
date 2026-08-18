"""Resolves each Norwegian entrant's UEFA qualifying cascade to blocked dates.

Built for issue #29 (modelling the Champions/Europa/Conference League
qualifying cascade) and issue #32 (conditional fixtures whose outcome is not
yet known). A cup round's opponent is unknown but its *place in the
competition* is guaranteed — every entered team is assumed to reach the
final (see `rounds/cup_schedule.py`). A European qualifying round is the
opposite: the opponent is often known (UEFA draws pairings well ahead of the
tie), but whether the team is even still in *this* round, rather than having
dropped into a different competition's equivalent round after losing the one
before, is exactly what is not known.

Each round is two legs (`EuropeanRound.first_leg`/`second_leg`), each with
its own `forced_date` or window — `resolve_all_legs` below turns those into
one real date per leg, the same way `rounds/cup_schedule.py` resolves a cup
round's date. A team's resolved commitment is then a handful of *specific*
dates (one per leg of every round reachable from its entry point, across
every branch of the cascade still open), not a blocked span — this is what
lets a normal European week (leg, league match, leg) work: the league match
only needs to clear `min_rest_days` from each leg date individually, not
stay outside a range covering the whole gap between them.

Rather than pick a cascade branch and risk being wrong, this resolves every
branch reachable from a team's entry point and blocks all of their leg
dates. Resolving a real result, once known, is a data edit: delete the
`EuropeanRound` on the branch not taken (or turn a leg's window into a
`forced_date` once confirmed) and regenerate — the domestic solver picks up
the narrower constraint on the next run, reshuffling any movable fixture
that now conflicts. That is issue #32's "harder case" (a conditional
fixture whose date, once triggered, is fixed and non-reschedulable, forcing
other fixtures to move) — no new mechanism is needed for it beyond what
`forced_date` and a solver re-run already do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..model.schema import Competition, EuropeanLeg, EuropeanRound, Season


class EuropeanCascadeError(Exception):
    """A team's European cascade cannot be resolved — a data problem (an
    entrant missing from every round of its own competition, or a
    `drop_to_round` with no matching round in the target competition), not
    something a solver retry can fix."""


@dataclass(frozen=True)
class EuropeanCommitmentDate:
    """One specific date a team is committed to — one leg of one round
    reachable from its entry point, across however many cascade branches are
    still open. `label` names the round and leg, for readable conflict
    events; `min_rest_days` is the owning competition's own value.
    """

    team_id: str
    date: date
    min_rest_days: int
    label: str


ResolvedLegs = dict[tuple[str, str], tuple[date, date]]


def resolve_leg_date(leg: EuropeanLeg, blackouts: set[date]) -> tuple[date, bool]:
    """One leg's window (or forced date) resolved to an actual date.

    A `forced_date` is used exactly, never drifted off a confirmed date onto
    a nearby day — the second return value flags a forced date that happens
    to be blacked out anyway, since that's a genuine data conflict worth a
    warning, not something this function can paper over. A window resolves
    to its own earliest non-blackout day, falling back to `window_start`
    itself (flagged) if every day in the window is blacked out.
    """
    if leg.is_forced:
        anchor = leg.forced_date
        return anchor, anchor in blackouts

    span = (leg.window_end - leg.window_start).days
    for offset in range(span + 1):
        candidate = leg.window_start + timedelta(days=offset)
        if candidate not in blackouts:
            return candidate, False
    return leg.window_start, True


def resolve_all_legs(
    competitions: list[Competition], blackouts: set[date]
) -> tuple[ResolvedLegs, list[str]]:
    """Resolve every round's two legs, across every competition given.

    Unlike `rounds/cup_schedule.py`, there is no round-to-round ordering or
    rest-gap check here: each `EuropeanRound`'s own dates come from real,
    independently-sourced UEFA scheduling data rather than being inferred
    from where the previous round landed, so there is nothing to derive —
    only each leg's own `forced_date`/window to resolve.
    """
    resolved: ResolvedLegs = {}
    warnings: list[str] = []
    for competition in competitions:
        for round_ in competition.european_rounds:
            first_date, first_blacked_out = resolve_leg_date(round_.first_leg, blackouts)
            second_date, second_blacked_out = resolve_leg_date(round_.second_leg, blackouts)
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


def resolve_team_cascade(
    team_id: str,
    home_competition: Competition,
    competitions_by_id: dict[str, Competition],
    resolved_legs: ResolvedLegs,
) -> list[EuropeanCommitmentDate]:
    """Every date `team_id` might play on, starting from its entry round in
    `home_competition`.

    Walks forward through two kinds of edge: win-progression (the next
    round in `home_competition.european_rounds`, if `team_id` is one of its
    entrants — advancing without ever consulting `drop_to_*`, since staying
    in the same competition needs no cascade pointer) and elimination
    (`drop_to_competition`/`drop_to_round`, when set). Both are followed at
    once — the walk does not know, and does not need to know, which one is
    real — collecting both legs' resolved dates from every round reached.
    """
    entry = _entry_round(team_id, home_competition)
    if entry is None:
        raise EuropeanCascadeError(
            f"{team_id!r} is not an entrant of any round in {home_competition.id!r}"
        )

    commitments: list[EuropeanCommitmentDate] = []
    frontier: list[tuple[Competition, EuropeanRound]] = [(home_competition, entry)]
    seen: set[tuple[str, str]] = set()

    while frontier:
        frontier = [(c, r) for c, r in frontier if (c.id, r.id) not in seen]
        if not frontier:
            break
        seen.update((c.id, r.id) for c, r in frontier)

        for competition, round_ in frontier:
            first_date, second_date = resolved_legs[(competition.id, round_.id)]
            commitments.append(
                EuropeanCommitmentDate(
                    team_id=team_id,
                    date=first_date,
                    min_rest_days=competition.min_rest_days,
                    label=f"{competition.name}: {round_.name} (first leg)",
                )
            )
            commitments.append(
                EuropeanCommitmentDate(
                    team_id=team_id,
                    date=second_date,
                    min_rest_days=competition.min_rest_days,
                    label=f"{competition.name}: {round_.name} (second leg)",
                )
            )

        next_frontier: list[tuple[Competition, EuropeanRound]] = []
        for competition, round_ in frontier:
            win_next = _next_round_for_team(team_id, competition, round_)
            if win_next is not None:
                next_frontier.append((competition, win_next))
            if round_.drop_to_competition is not None:
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
                next_frontier.append((drop_competition, drop_round))
        frontier = next_frontier

    return commitments


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

    Scans forward rather than only checking the very next list entry: a
    competition's `european_rounds` can interleave rounds from more than
    one UEFA path (Champions Path vs. League Path/Main Path — see
    `champions_league_2026.yml`'s header), so the round immediately after
    `round_` in list order may belong to a path `team_id` isn't on. Only
    checking the next entry would truncate the walk there instead of
    finding the team's actual next round further down the list.
    """
    rounds = competition.european_rounds
    index = next(i for i, r in enumerate(rounds) if r.id == round_.id)
    for following in rounds[index + 1 :]:
        if team_id in following.entrants:
            return following
    return None


def resolve_european_commitments(
    competitions: list[Competition], season: Season
) -> tuple[dict[str, list[EuropeanCommitmentDate]], list[str]]:
    """Resolve every team's cascade across every European competition given.

    `competitions` should be every `format == "european"` competition in
    the season, so a `drop_to_competition` pointer always has somewhere to
    resolve to. `season.global_blackouts` is the only blackout source
    consulted — European qualifiers don't book venues, so venue-specific
    blackouts don't apply, same as `rounds/cup_schedule.py::schedule_cups`.

    Only a team's *true* entry competition is walked from: a round that is
    itself the target of some other round's `drop_to_competition`/
    `drop_to_round` is reached by that cascade walk, not treated as a
    second, independent home for the team(s) who cascade into it — starting
    from both would silently overwrite the correct (longer) commitment list
    with the incomplete one from the cascade's midpoint. The skip is keyed
    on *(team, round)*, not just *round*: a round can simultaneously be one
    team's real, direct entry point and another team's cascade landing
    spot, and keying on the round alone would incorrectly skip the direct
    entrant too.
    """
    blackouts = {b.date for b in season.global_blackouts}
    resolved_legs, warnings = resolve_all_legs(competitions, blackouts)

    drop_targets = {
        (team_id, round_.drop_to_competition, round_.drop_to_round)
        for competition in competitions
        for round_ in competition.european_rounds
        if round_.drop_to_competition is not None
        for team_id in round_.entrants
    }
    by_id = {c.id: c for c in competitions}

    commitments_by_team: dict[str, list[EuropeanCommitmentDate]] = {}
    for competition in competitions:
        for team_id in competition.teams:
            entry = _entry_round(team_id, competition)
            if entry is None:
                continue
            if (team_id, competition.id, entry.id) in drop_targets:
                continue
            commitments_by_team[team_id] = resolve_team_cascade(
                team_id, competition, by_id, resolved_legs
            )
    return commitments_by_team, warnings


def european_conflict(
    commitments_by_team: dict[str, list[EuropeanCommitmentDate]], team_id: str, day: date
) -> bool:
    """Whether `day` falls within `min_rest_days` of one of `team_id`'s
    resolved European commitment dates.

    `cup_conflict`'s counterpart — point-date-plus-rest, the same shape and
    the same arithmetic, just reading from a resolved European cascade
    instead of a resolved cup round. Shared by `solvers/greedy.py`'s
    construction heuristic (steering initial placement away from a
    commitment date, the same way it already does for `cup_conflict`) and,
    ultimately, by `EuropeanCommitmentConflict` in `scoring/hard.py`, which
    is the authoritative check a schedule is actually judged against.
    """
    return any(
        abs((day - commitment.date).days) - 1 < commitment.min_rest_days
        for commitment in commitments_by_team.get(team_id, ())
    )


__all__ = [
    "EuropeanCascadeError",
    "EuropeanCommitmentDate",
    "european_conflict",
    "resolve_all_legs",
    "resolve_european_commitments",
    "resolve_leg_date",
    "resolve_team_cascade",
]
