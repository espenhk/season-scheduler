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
    events; `min_rest_days` is the owning competition's own value;
    `competition_id` is the owning competition's id, for matching against a
    main tournament's `reachable_from` without depending on `label`'s
    free-text competition name.

    `certain` (issue #93) is False for a commitment reachable only via one
    of several mutually-exclusive cascade branches — the entry round is
    always certain (a team definitely plays it, whatever happens next), and
    certainty carries forward unchanged down a branch that never forks, but
    the moment a round offers more than one live continuation (both
    win-progression and a `drop_to_competition`/`drop_to_round`), every
    commitment reachable only through one of those alternatives is no
    longer certain — and, once uncertain, stays that way for the rest of
    that branch, since not knowing you're even on it makes anything further
    down it just as conditional. `EuropeanCommitmentConflict` (hard) and its
    soft counterpart in `scoring/soft.py` split on this flag.
    """

    team_id: str
    date: date
    min_rest_days: int
    label: str
    competition_id: str
    certain: bool = True


ResolvedLegs = dict[tuple[str, str], tuple[date, date]]


def resolve_leg_date(
    leg: EuropeanLeg | EuropeanMatchday,
    blackouts: set[date],
    preferred_weekday: Weekday | None = None,
) -> tuple[date, bool]:
    """One leg's window (or forced date) resolved to an actual date.

    A `forced_date` is used exactly, never drifted off a confirmed date onto
    a nearby day — the second return value flags a forced date that happens
    to be blacked out anyway, since that's a genuine data conflict worth a
    warning, not something this function can paper over. A window resolves
    to its own earliest non-blackout day, falling back to `window_start`
    itself (flagged) if every day in the window is blacked out.

    `preferred_weekday` (issue #79 — main tournaments follow a per-competition
    desired matchday, e.g. Champions League on Thursdays) biases a *window*
    resolution towards its first matching, non-blacked-out day instead of the
    plain earliest one; a `forced_date` is never overridden by it, since a
    confirmed date is confirmed regardless of which weekday it falls on. Falls
    back to the plain earliest-day search when no day in the window matches
    (or no preference was given), so this stays a strict superset of the old
    behaviour rather than a second code path.
    """
    if leg.is_forced:
        anchor = leg.forced_date
        return anchor, anchor in blackouts

    span = (leg.window_end - leg.window_start).days
    if preferred_weekday is not None:
        target = WEEKDAYS.index(preferred_weekday)
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

    A round that offers only one live continuation (win-progression alone,
    or a drop alone) keeps the walk's current certainty going into it. A
    round that offers both is a genuine fork — the two outcomes are
    mutually exclusive, so everything reached only via one of them
    (`EuropeanCommitmentDate.certain=False`) stops counting as a guaranteed
    commitment; the entry round itself, reached before any fork, is always
    certain (issue #93).
    """
    entry = _entry_round(team_id, home_competition)
    if entry is None:
        raise EuropeanCascadeError(
            f"{team_id!r} is not an entrant of any round in {home_competition.id!r}"
        )

    commitments: list[EuropeanCommitmentDate] = []
    frontier: list[tuple[Competition, EuropeanRound, bool]] = [(home_competition, entry, True)]
    seen: set[tuple[str, str]] = set()

    while frontier:
        frontier = [(c, r, certain) for c, r, certain in frontier if (c.id, r.id) not in seen]
        if not frontier:
            break
        seen.update((c.id, r.id) for c, r, _ in frontier)

        for competition, round_, certain in frontier:
            first_date, second_date = resolved_legs[(competition.id, round_.id)]
            commitments.append(
                EuropeanCommitmentDate(
                    team_id=team_id,
                    date=first_date,
                    min_rest_days=competition.min_rest_days,
                    label=f"{competition.name}: {round_.name} (first leg)",
                    competition_id=competition.id,
                    certain=certain,
                )
            )
            commitments.append(
                EuropeanCommitmentDate(
                    team_id=team_id,
                    date=second_date,
                    min_rest_days=competition.min_rest_days,
                    label=f"{competition.name}: {round_.name} (second leg)",
                    competition_id=competition.id,
                    certain=certain,
                )
            )

        next_frontier: list[tuple[Competition, EuropeanRound, bool]] = []
        for competition, round_, certain in frontier:
            branches: list[tuple[Competition, EuropeanRound]] = []

            win_next = _next_round_for_team(team_id, competition, round_)
            if win_next is not None:
                branches.append((competition, win_next))
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
                branches.append((drop_competition, drop_round))

            forked = len(branches) > 1
            next_frontier.extend(
                (next_competition, next_round, certain and not forked)
                for next_competition, next_round in branches
            )
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


def resolve_main_tournament_dates(
    competition: Competition, blackouts: set[date]
) -> tuple[list[tuple[str, date]], list[str]]:
    """Every league-phase matchday and knockout-round leg date for one main
    tournament competition (issue #79), each paired with a display label.

    Unlike qualifying's cascade, there is no branching to walk here: every
    reachable team is assumed able to reach the final (see
    `Competition.reachable_from`), so a main tournament's own dates don't
    depend on which team is asking — they are resolved once per competition
    and the same list is blocked for every reachable team.
    """
    dated: list[tuple[str, date]] = []
    warnings: list[str] = []

    for matchday in competition.league_phase_matchdays:
        resolved, blacked_out = resolve_leg_date(matchday, blackouts, competition.preferred_weekday)
        dated.append((f"{competition.name}: {matchday.name}", resolved))
        if blacked_out:
            warnings.append(
                f"{competition.name}: {matchday.name} has no legal (non-blackout) date in its "
                f"window — placed on {resolved} anyway"
            )

    for round_ in competition.knockout_rounds:
        first_date, first_blacked_out = resolve_leg_date(
            round_.first_leg, blackouts, competition.preferred_weekday
        )
        first_label = (
            f"{competition.name}: {round_.name}"
            if round_.second_leg is None
            else f"{competition.name}: {round_.name} (first leg)"
        )
        dated.append((first_label, first_date))
        if first_blacked_out:
            warnings.append(
                f"{competition.name}: {round_.name} has no legal (non-blackout) date in its "
                f"window — placed on {first_date} anyway"
            )
        if round_.second_leg is not None:
            second_date, second_blacked_out = resolve_leg_date(
                round_.second_leg, blackouts, competition.preferred_weekday
            )
            dated.append((f"{competition.name}: {round_.name} (second leg)", second_date))
            if second_blacked_out:
                warnings.append(
                    f"{competition.name}: {round_.name} second leg has no legal (non-blackout) "
                    f"date in its window — placed on {second_date} anyway"
                )

    return dated, warnings


def _reachable_teams(competition: Competition, by_id: dict[str, Competition]) -> set[str]:
    """Every team reachable for `competition` (a main tournament) — the
    union of every entrant of every round of every qualifying competition
    named in `reachable_from`. Shared by `resolve_main_tournament_commitments`
    (what to block) and `build_main_tournament_rounds_for_display` (who to
    show as a synthetic entrant in the report)."""
    reachable: set[str] = set()
    for source_id in competition.reachable_from:
        source = by_id.get(source_id)
        if source is None:
            continue  # loader validation already catches a dangling reference
        for round_ in source.european_rounds:
            reachable.update(round_.entrants)
    return reachable


def _reachable_with_certainty(
    team_id: str,
    competition: Competition,
    qualifying_commitments_by_team: dict[str, list[EuropeanCommitmentDate]],
) -> bool:
    """Whether `team_id` is guaranteed to reach `competition` (a main
    tournament), rather than only via one of several mutually-exclusive
    qualifying branches (issue #93).

    `qualifying_commitments_by_team` is `resolve_qualifying_commitments`'s
    result — each of a team's qualifying commitments already carries the
    id of the competition it belongs to and whether the cascade branch that
    reached it was certain (see `EuropeanCommitmentDate.certain`). A source
    competition named in `reachable_from` counts as a guaranteed route in
    if the team has at least one commitment there and every one of them is
    certain; a source the team only reaches via a fork (or doesn't reach at
    all) doesn't. One guaranteed route among several named sources is
    enough — a team can't be *more* than certain to arrive.
    """
    commitments = qualifying_commitments_by_team.get(team_id, ())
    for source_id in competition.reachable_from:
        source_commitments = [c for c in commitments if c.competition_id == source_id]
        if source_commitments and all(c.certain for c in source_commitments):
            return True
    return False


def resolve_main_tournament_commitments(
    competitions: list[Competition],
    season: Season,
    qualifying_commitments_by_team: dict[str, list[EuropeanCommitmentDate]],
) -> tuple[dict[str, list[EuropeanCommitmentDate]], list[str]]:
    """Block every reachable team's dates for every main tournament (issue
    #79) among `competitions`.

    A team is "reachable" for a main tournament if it entered any round of
    any qualifying competition named in that main tournament's
    `reachable_from` — see that field's docstring for the "assume it goes
    all the way" simplification this makes. `competitions` should be every
    `format == "european"` competition in the season, qualifying and main
    tournament alike, the same as `resolve_european_commitments` expects.

    `qualifying_commitments_by_team` — `resolve_qualifying_commitments`'s
    result for the same `competitions` — decides whether each reachable
    team's block is certain (hard) or only reachable via a cascade fork
    (soft, issue #93): a team reachable from more than one main tournament
    only via mutually-exclusive branches (e.g. Tromsø IL, one hop from
    Europa League qualifying into the Conference League play-off) no longer
    blocks its whole domestic calendar against both tournaments' full
    season as if both were certain.
    """
    blackouts = set(season.blacked_out_dates)
    by_id = {c.id: c for c in competitions}

    commitments_by_team: dict[str, list[EuropeanCommitmentDate]] = {}
    warnings: list[str] = []

    for competition in competitions:
        if not competition.is_main_tournament:
            continue
        dated, competition_warnings = resolve_main_tournament_dates(competition, blackouts)
        warnings.extend(competition_warnings)

        for team_id in sorted(_reachable_teams(competition, by_id)):
            certain = _reachable_with_certainty(team_id, competition, qualifying_commitments_by_team)
            commitments_by_team.setdefault(team_id, []).extend(
                EuropeanCommitmentDate(
                    team_id=team_id,
                    date=resolved,
                    min_rest_days=competition.min_rest_days,
                    label=label,
                    competition_id=competition.id,
                    certain=certain,
                )
                for label, resolved in dated
            )

    return commitments_by_team, warnings


def build_main_tournament_rounds_for_display(
    competition: Competition, competitions: list[Competition], blackouts: set[date]
) -> tuple[list[EuropeanRound], ResolvedLegs]:
    """Adapts a main tournament's `league_phase_matchdays`/`knockout_rounds`
    into the same `EuropeanRound`/`ResolvedLegs` shape `resolve_all_legs`
    produces for a qualifying competition, so `report/render.py`'s existing
    `_european_views`/`_european_fixtures`/`_european_entries` can display a
    main tournament without any changes of their own.

    A main tournament's actual pairings aren't modelled — only its dates —
    so every reachable team (`_reachable_teams`) becomes a synthetic
    `EuropeanTie` with the same team on every synthetic round, opponent
    left at `EuropeanTie`'s own "TBD" default and `home_leg` left `None`
    (`_european_venue_type` already renders that as "unknown", the same
    "not yet confirmed" state a qualifying tie without a drawn pairing
    shows). A single-match knockout round (the final, `second_leg is
    None`) becomes a synthetic round whose two legs share one date, the
    same shape `_european_round_date_label` already collapses to a single
    displayed date for a same-day double round; its `venue_name` (a real,
    fixed venue this project doesn't otherwise have anywhere to show) is
    folded into the synthetic round's `note` instead of taught to
    `_european_venue_type`/`_european_venue_label`, which are shaped around
    a *team's* venue, not a neutral one — same reason `venue_type` here
    stays "unknown" rather than cup_schedule.py's "neutral": nothing
    downstream distinguishes a synthetic tie's ground from a real one's.
    """
    by_id = {c.id: c for c in competitions}
    ties = [EuropeanTie(team=team_id) for team_id in sorted(_reachable_teams(competition, by_id))]

    rounds: list[EuropeanRound] = []
    resolved_legs: ResolvedLegs = {}

    for matchday in competition.league_phase_matchdays:
        resolved_date, _ = resolve_leg_date(matchday, blackouts, competition.preferred_weekday)
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
        first_date, _ = resolve_leg_date(round_.first_leg, blackouts, competition.preferred_weekday)
        if round_.second_leg is None:
            second_date = first_date
        else:
            second_date, _ = resolve_leg_date(
                round_.second_leg, blackouts, competition.preferred_weekday
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


def resolve_qualifying_commitments(
    competitions: list[Competition], season: Season
) -> tuple[dict[str, list[EuropeanCommitmentDate]], list[str]]:
    """Resolve every team's qualifying cascade across every European
    competition given — `resolve_european_commitments` minus the main
    tournament merge, split out so `resolve_main_tournament_commitments`
    can consult each team's qualifying-cascade certainty (issue #93)
    without recomputing it.

    `competitions` should be every `format == "european"` competition in
    the season, so a `drop_to_competition` pointer always has somewhere to
    resolve to. `season.blacked_out_dates` (global blackouts plus
    `global_blackout_ranges`) is the only blackout source consulted —
    European qualifiers don't book venues, so venue-specific blackouts
    (`venue_blackouts`/`venue_blackout_ranges`) don't apply, same as
    `rounds/cup_schedule.py::schedule_cups`.

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

    Main tournament competitions (issue #79 — `Competition.is_main_tournament`)
    have no `european_rounds` of their own, so they never contribute an entry
    point here.
    """
    blackouts = set(season.blacked_out_dates)
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


def resolve_european_commitments(
    competitions: list[Competition], season: Season
) -> tuple[dict[str, list[EuropeanCommitmentDate]], list[str]]:
    """Resolve every team's cascade across every European competition given,
    qualifying and main tournament alike.

    `resolve_qualifying_commitments` does the qualifying-cascade walk;
    `resolve_main_tournament_commitments` resolves each main tournament's
    own dates and merges them in below, keyed by the same team ids the
    qualifying walk already produced entries for (every team reachable for
    a main tournament necessarily entered some qualifying competition's
    rounds, or it wouldn't be reachable at all).
    """
    commitments_by_team, warnings = resolve_qualifying_commitments(competitions, season)

    main_commitments, main_warnings = resolve_main_tournament_commitments(
        competitions, season, commitments_by_team
    )
    for team_id, commitments in main_commitments.items():
        commitments_by_team.setdefault(team_id, []).extend(commitments)
    warnings.extend(main_warnings)

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
    "build_main_tournament_rounds_for_display",
    "european_conflict",
    "resolve_all_legs",
    "resolve_european_commitments",
    "resolve_leg_date",
    "resolve_main_tournament_commitments",
    "resolve_main_tournament_dates",
    "resolve_qualifying_commitments",
    "resolve_team_cascade",
]
