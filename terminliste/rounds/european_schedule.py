"""Resolves each Norwegian entrant's UEFA qualifying cascade to blocked dates
(#29, #32).

A cup round's opponent is unknown but its place in the competition is taken as
given (see `rounds/cup_schedule.py`). A European qualifying round is the
opposite: the opponent is often drawn well ahead, but whether the team is
still in *this* round rather than having dropped into a lower competition's
equivalent is exactly what isn't known.

Each round is two legs, each with its own `forced_date` or window, which
`resolve_all_legs` turns into one real date apiece. A team's commitment is
therefore a handful of *specific* dates, not a blocked span — that is what
lets a normal European week (leg, league match, leg) work, since the league
match need only clear `min_rest_days` from each leg individually.

Rather than pick a cascade branch and risk being wrong, every branch reachable
from a team's entry point is resolved and all its leg dates blocked. Once a
real result is known, resolving it is a data edit: delete the rounds on the
branch not taken, or narrow a leg's window to a `forced_date`, and regenerate.
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
    """One specific date a team is committed to: one leg of one reachable
    round. `competition_id` is carried separately from the free-text `label`
    so a main tournament's `reachable_from` can be matched against it.

    `certain` (#93) is False for a commitment reachable only via one of
    several mutually-exclusive cascade branches. The entry round is always
    certain, and certainty carries down a branch that never forks; once a
    round offers both win-progression and a drop, everything beyond either is
    uncertain and stays that way. The hard and soft European constraints split
    on this flag.
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

    A `forced_date` is used exactly; the second return value flags one that
    happens to be blacked out, which is a data conflict worth a warning rather
    than something to paper over. A window resolves to its earliest
    non-blackout day, falling back to `window_start` (flagged) if every day is
    blacked out.

    `preferred_weekday` (#79) biases a *window* towards its first matching
    non-blacked-out day — a confirmed date is confirmed whatever weekday it
    falls on, so a `forced_date` ignores it, as does a window with no matching
    day.
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

    No round-to-round ordering or rest-gap check, unlike a cup: each round's
    dates come from real UEFA scheduling data rather than being inferred from
    where the previous round landed.
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

    Follows two kinds of edge at once — win-progression (the next round of
    `home_competition` this team enters) and elimination
    (`drop_to_competition`/`drop_to_round`) — since the walk cannot know
    which is real, collecting both legs of every round reached.

    A round offering only one continuation keeps the walk's current
    certainty. One offering both is a genuine fork, so everything beyond
    either becomes uncertain; the entry round, reached before any fork, is
    always certain (#93).
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


def resolve_main_tournament_dates(
    competition: Competition, blackouts: set[date]
) -> tuple[list[tuple[str, date]], list[str]]:
    """Every league-phase matchday and knockout-round leg date for one main
    tournament (#79), each with a display label.

    No branching to walk: every reachable team is assumed able to reach the
    final (see `Competition.reachable_from`), so the dates don't depend on
    which team is asking and one resolved list serves all of them.
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
    """Every team reachable for a main tournament: the union of every entrant
    of every round of every qualifying competition in `reachable_from`."""
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
    """Whether `team_id` is guaranteed to reach a main tournament, rather
    than only via one of several mutually-exclusive qualifying branches (#93).

    A `reachable_from` source counts as a guaranteed route in if the team has
    commitments there and every one is certain. One such route among several
    sources is enough — a team can't be more than certain to arrive.
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
    """Block every reachable team's dates for every main tournament (#79)
    among `competitions` — which should be every European competition in the
    season, qualifying and main tournament alike.

    A team is reachable if it entered any round of any qualifying competition
    in the tournament's `reachable_from`. `qualifying_commitments_by_team`
    decides whether each block is certain (hard) or only reachable via a
    cascade fork (soft, #93), so a team reachable from two tournaments only
    via mutually-exclusive branches doesn't block its domestic calendar
    against both full seasons.
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
    tournament merge, split out so `resolve_main_tournament_commitments` can
    consult each team's certainty (#93) without recomputing it.

    `competitions` should be every European competition in the season, so a
    `drop_to_competition` pointer always resolves. Only global blackouts are
    consulted: European qualifiers book no venues.

    Only a team's *true* entry competition is walked from — a round that is
    some other round's drop target is reached by that walk, and starting from
    it too would overwrite the correct commitment list with the incomplete one
    from the cascade's midpoint. The skip is keyed on (team, round), since one
    round can be one team's direct entry point and another's landing spot.
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
    qualifying and main tournament alike."""
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

    `cup_conflict`'s counterpart, used by the greedy placer to steer initial
    placement; `EuropeanCommitmentConflict` is the authoritative check.
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
