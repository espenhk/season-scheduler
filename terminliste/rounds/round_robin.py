"""Round-robin pairing generation — who plays whom, in what order.

Deliberately separate from date assignment. This half is pure combinatorics:
deterministic, fast, and correct by construction. The calendar half is the
search problem. Keeping them apart means the solver can shuffle dates freely
without ever risking an invalid tournament.

The circle method (Berger tables) pairs n teams over n-1 rounds: fix one team,
rotate the rest. For odd n a bye slot is added, and the team drawn against it
sits that round out. The second leg is the mirror of the first with home and
away reversed, which is what makes "everyone meets once before anyone meets
twice" true by construction rather than by constraint.
"""

from __future__ import annotations

from ..model.schema import Fixture


def circle_method_rounds(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Single round-robin pairings: `rounds[i]` is a list of (home, away).

    Home/away here is provisional — `assign_home_away` rewrites it to minimise
    breaks. What this guarantees is the pairing structure: every team meets
    every other exactly once, and appears at most once per round.
    """
    if len(teams) < 2:
        return []

    roster = list(teams)
    bye: str | None = None
    if len(roster) % 2 == 1:
        bye = "__bye__"
        roster.append(bye)

    n = len(roster)
    fixed, rotating = roster[0], roster[1:]

    rounds: list[list[tuple[str, str]]] = []
    for round_index in range(n - 1):
        pairs: list[tuple[str, str]] = []

        # The fixed team alternates home and away so it does not end up playing
        # every match of the season at one end.
        opponent = rotating[0]
        if round_index % 2 == 0:
            pairs.append((fixed, opponent))
        else:
            pairs.append((opponent, fixed))

        for i in range(1, n // 2):
            a = rotating[i]
            b = rotating[len(rotating) - i]
            if round_index % 2 == 0:
                pairs.append((a, b))
            else:
                pairs.append((b, a))

        if bye is not None:
            pairs = [(h, a) for h, a in pairs if bye not in (h, a)]

        rounds.append(pairs)
        rotating = [rotating[-1]] + rotating[:-1]

    return rounds


def assign_home_away(rounds: list[list[tuple[str, str]]]) -> list[list[tuple[str, str]]]:
    """Rebalance home/away within each round to even out each team's tally.

    The circle method alternates, but its rotation still leaves some teams
    lopsided. Flipping any pairing that brings both teams closer to even is a
    cheap greedy improvement on the raw circle output.

    Only orientation changes, so round-robin validity is preserved.
    """
    balance: dict[str, int] = {}
    for pairs in rounds:
        for home, away in pairs:
            balance.setdefault(home, 0)
            balance.setdefault(away, 0)

    rebalanced: list[list[tuple[str, str]]] = []
    for pairs in rounds:
        round_pairs: list[tuple[str, str]] = []
        for home, away in pairs:
            # balance[t] is home matches minus away matches so far, so the
            # more home-heavy side gives up the fixture.
            if balance[home] > balance[away]:
                home, away = away, home
            balance[home] += 1
            balance[away] -= 1
            round_pairs.append((home, away))
        rebalanced.append(round_pairs)
    return rebalanced


def generate_fixtures(
    competition_id: str, teams: list[str], rounds_per_pairing: int = 2
) -> list[Fixture]:
    """Full fixture list for a competition, ordered by round.

    For `rounds_per_pairing=2` the second leg mirrors the first with home and
    away reversed, so every pair has met once before any pair meets twice.
    Higher values keep alternating — odd legs share leg 1's orientation, even
    legs are its mirror.

    That alternation is what keeps an odd `rounds_per_pairing` fair with no
    extra balancing. Each (orientation, mirror) leg pair cancels exactly for
    every team, leaving at most one unpaired leg, which repeats leg 1's
    orientation — already balanced by `assign_home_away`. So a team's home
    total is `(rounds_per_pairing // 2) * (n - 1) + leg_1_home_count`, and
    since `leg_1_home_count` is within 1 of `(n - 1) / 2`, so is the
    season-total split.
    """
    if len(teams) < 2:
        return []

    base = assign_home_away(circle_method_rounds(teams))
    rounds_per_leg = len(base)

    fixtures: list[Fixture] = []
    for leg in range(1, rounds_per_pairing + 1):
        reverse = leg % 2 == 0
        for local_round, pairs in enumerate(base):
            round_index = (leg - 1) * rounds_per_leg + local_round
            for home, away in pairs:
                if reverse:
                    home, away = away, home
                fixtures.append(
                    Fixture(
                        competition_id=competition_id,
                        home_team=home,
                        away_team=away,
                        leg=leg,
                        round_index=round_index,
                    )
                )
    return fixtures


def fixtures_by_round(fixtures: list[Fixture]) -> dict[int, list[Fixture]]:
    grouped: dict[int, list[Fixture]] = {}
    for fixture in fixtures:
        grouped.setdefault(fixture.round_index, []).append(fixture)
    return grouped


def swap_teams(fixtures: list[Fixture], team_a: str, team_b: str) -> list[Fixture]:
    """Return a copy with two teams exchanged throughout.

    A relabelling, so the result is always a valid round-robin, but it
    relocates both teams' entire seasons — the highest-leverage move the local
    search has.
    """
    swapped: list[Fixture] = []
    for fixture in fixtures:
        home = _swap(fixture.home_team, team_a, team_b)
        away = _swap(fixture.away_team, team_a, team_b)
        swapped.append(fixture.model_copy(update={"home_team": home, "away_team": away}))
    return swapped


def _swap(team: str, a: str, b: str) -> str:
    if team == a:
        return b
    if team == b:
        return a
    return team
