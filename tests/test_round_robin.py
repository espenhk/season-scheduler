"""Exhaustive checks on the pure-combinatorics round-robin layer.

These never touch dates, venues, or scoring — if any of these fail, the bug is
in `terminliste/rounds/round_robin.py`, not in the solver or the data.
"""

from __future__ import annotations

from collections import Counter

import pytest

from terminliste.rounds.round_robin import (
    circle_method_rounds,
    generate_fixtures,
    swap_teams,
)


@pytest.mark.parametrize("n", range(2, 21))
def test_circle_method_pairs_everyone_exactly_once(n):
    teams = [f"t{i}" for i in range(n)]
    rounds = circle_method_rounds(teams)

    pairs = Counter()
    for round_pairs in rounds:
        seen_this_round: set[str] = set()
        for home, away in round_pairs:
            assert home not in seen_this_round, f"n={n}: {home} appears twice in one round"
            assert away not in seen_this_round, f"n={n}: {away} appears twice in one round"
            seen_this_round.add(home)
            seen_this_round.add(away)
            pairs[frozenset((home, away))] += 1

    expected_pairs = n * (n - 1) // 2
    assert len(pairs) == expected_pairs
    assert all(count == 1 for count in pairs.values())


@pytest.mark.parametrize("n", range(2, 21))
def test_circle_method_round_count(n):
    teams = [f"t{i}" for i in range(n)]
    rounds = circle_method_rounds(teams)
    expected = (n - 1) if n % 2 == 0 else n
    assert len(rounds) == expected


@pytest.mark.parametrize("n", [3, 5, 7, 9])
def test_odd_n_gives_each_team_exactly_one_bye(n):
    teams = [f"t{i}" for i in range(n)]
    rounds = circle_method_rounds(teams)
    byes = Counter()
    for round_pairs in rounds:
        playing = {t for pair in round_pairs for t in pair}
        for team in teams:
            if team not in playing:
                byes[team] += 1
    assert all(count == 1 for count in byes.values()), byes


def test_double_league_every_pair_meets_twice_home_and_away():
    teams = [f"t{i}" for i in range(8)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=2)

    oriented = Counter((f.home_team, f.away_team) for f in fixtures)
    assert len(oriented) == 8 * 7
    assert all(count == 1 for count in oriented.values())

    unordered = Counter(frozenset((f.home_team, f.away_team)) for f in fixtures)
    assert all(count == 2 for count in unordered.values())


def test_first_leg_completes_before_second_leg_starts():
    teams = [f"t{i}" for i in range(10)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=2)
    leg1_rounds = [f.round_index for f in fixtures if f.leg == 1]
    leg2_rounds = [f.round_index for f in fixtures if f.leg == 2]
    assert max(leg1_rounds) < min(leg2_rounds)


def test_single_league_has_one_leg():
    teams = [f"t{i}" for i in range(6)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=1)
    assert all(f.leg == 1 for f in fixtures)
    assert len(fixtures) == 6 * 5 // 2


def test_home_matches_are_evenly_split_for_even_team_counts():
    teams = [f"t{i}" for i in range(16)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=2)
    home_counts = Counter(f.home_team for f in fixtures)
    # Double league, even team count: everyone ends up with exactly n-1 home
    # matches (one leg home, one leg away, per opponent).
    assert set(home_counts.values()) == {15}


@pytest.mark.parametrize("n", [8, 12, 16])
def test_triple_league_every_pair_meets_three_times_with_a_two_one_split(n):
    teams = [f"t{i}" for i in range(n)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=3)

    unordered = Counter(frozenset((f.home_team, f.away_team)) for f in fixtures)
    assert len(unordered) == n * (n - 1) // 2
    assert all(count == 3 for count in unordered.values())

    # Every pairing splits 2-1 one way or the other — never 3-0.
    home_side_counts: dict[frozenset[str], Counter] = {}
    for f in fixtures:
        key = frozenset((f.home_team, f.away_team))
        home_side_counts.setdefault(key, Counter())[f.home_team] += 1
    for key, sides in home_side_counts.items():
        assert sorted(sides.values()) == [1, 2], f"{key}: {sides}"


@pytest.mark.parametrize("n", [8, 12, 16])
def test_triple_league_home_totals_are_balanced_within_one(n):
    """Legs 1 and 3 share an orientation, leg 2 is the mirror of both.

    Every team's home tally from legs 1+2 cancels exactly (each opponent
    contributes one home and one away across those two legs, regardless of
    orientation), so the only thing that can unbalance a team's season total
    is leg 3 — which is the *same* single-leg assignment as leg 1, already
    balanced by `assign_home_away`. That is what keeps a triple round-robin
    fair without needing a season-aware balancing pass of its own.
    """
    teams = [f"t{i}" for i in range(n)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=3)

    home_counts = Counter(f.home_team for f in fixtures)
    away_counts = Counter(f.away_team for f in fixtures)
    for team in teams:
        assert home_counts[team] + away_counts[team] == 3 * (n - 1)
        assert abs(home_counts[team] - away_counts[team]) <= 1

    assert max(home_counts.values()) - min(home_counts.values()) <= 1


def test_swap_teams_preserves_round_robin_validity():
    teams = [f"t{i}" for i in range(8)]
    fixtures = generate_fixtures("comp", teams, rounds_per_pairing=2)
    swapped = swap_teams(fixtures, "t2", "t5")

    oriented = Counter((f.home_team, f.away_team) for f in swapped)
    assert len(oriented) == 8 * 7
    assert all(count == 1 for count in oriented.values())
    # t2 and t5 have fully traded places.
    assert {f.home_team for f in fixtures if f.home_team == "t2"}
    original_t2_rounds = {f.round_index for f in fixtures if "t2" in (f.home_team, f.away_team)}
    swapped_t5_rounds = {f.round_index for f in swapped if "t5" in (f.home_team, f.away_team)}
    assert original_t2_rounds == swapped_t5_rounds
