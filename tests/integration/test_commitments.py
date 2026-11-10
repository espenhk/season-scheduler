"""Elimination-aware commitments on the real 2026 data (#8)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from terminliste.frame import build_season_frame
from terminliste.model.schema import ContingencyPolicy


@pytest.fixture(scope="module")
def frame(world, season):
    return build_season_frame(world, season)[0]


def _window(world, season):
    league = world.competition("eliteserien_2026")
    start, end = league.start or season.start, league.end or season.end
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _blocked_days(frame, days, team_id, source=None):
    return sum(
        1
        for day in days
        if any(
            c.clashes(day) for c in frame.commitments.hard_for(team_id) if source in (None, c.source)
        )
    )


# Over the Eliteserien window with every cup round and every UEFA leg a hard
# reservation (the model before elimination-aware commitments), Brann blocked
# 77 days, Bodø/Glimt 73, Viking 63, Lillestrøm 62, Tromsø 50 and the other
# eleven clubs 30 each: 655 team-days. Entry rounds only (#8):
EXPECTED_BLOCKED_DAYS = {
    "bodo_glimt_m": 15,
    "brann_m": 15,
    "tromso_m": 15,
    "lillestrom_m": 12,
    "viking_m": 12,
}
OTHER_CLUB_BLOCKED_DAYS = 5
EXPECTED_LEAGUE_TOTAL = 124


def test_blocked_days_per_team_with_only_entry_rounds_hard(world, season, frame):
    days = _window(world, season)
    league = world.competition("eliteserien_2026")
    counts = {team: _blocked_days(frame, days, team) for team in league.teams}

    for team, expected in EXPECTED_BLOCKED_DAYS.items():
        assert counts[team] == expected, team
    for team in set(league.teams) - set(EXPECTED_BLOCKED_DAYS):
        assert counts[team] == OTHER_CLUB_BLOCKED_DAYS, team
    assert sum(counts.values()) == EXPECTED_LEAGUE_TOTAL


def test_a_lower_threshold_blocks_more_days(world, season):
    relaxed = season.model_copy(update={"contingency_policy": ContingencyPolicy(hard_threshold=0.5)})
    frame, _ = build_season_frame(world, relaxed)
    days = _window(world, season)
    assert _blocked_days(frame, days, "brann_m") > EXPECTED_BLOCKED_DAYS["brann_m"]


def test_only_entry_rounds_are_hard_by_default(frame):
    hard_nodes = {c.node_id for c in frame.commitments if c.hard}
    assert hard_nodes == {
        # UEFA entry rounds
        "bodo_glimt_m/champions_league_2026/q3",
        "viking_m/champions_league_2026/playoff",
        "tromso_m/europa_league_2026/q2",
        "lillestrom_m/europa_league_2026/playoff",
        "brann_m/conference_league_2026/q2",
        "brann_w/womens_champions_league_2026/q2",
        "valerenga_w/womens_champions_league_2026/q2",
        "rosenborg_w/womens_europa_cup_2026/q1",
        # Each cup's first round for the Eliteserien clubs
        *(f"{t}/cup_men_2026/r4" for t in frame.cup_trees if t.endswith("_m")),
        *(f"{t}/cup_men_2027/r1" for t in frame.cup_trees if t.endswith("_m")),
        *(f"{t}/cup_women_2026/r2" for t in frame.cup_trees if t.endswith("_w")),
        *(f"{t}/cup_women_2027/r2" for t in frame.cup_trees if t.endswith("_w")),
    }
    assert all(c.reached_probability == 1.0 for c in frame.commitments if c.hard)


def test_lillestrom_europa_and_conference_league_phases_are_mutually_exclusive(frame):
    commitments = [c for c in frame.commitments if c.team_id == "lillestrom_m" and c.source == "uefa"]
    europa = [c for c in commitments if c.competition_id == "europa_league_main_2026"]
    conference = [c for c in commitments if c.competition_id == "conference_league_main_2026"]
    assert europa and conference

    for e in europa:
        for u in conference:
            assert u.node_id in e.exclusive_with
            assert e.node_id in u.exclusive_with
    # Their play-off is the entry round they both follow: not exclusive with either.
    playoff = [c for c in commitments if c.competition_id == "europa_league_2026"]
    assert all(not c.exclusive_with for c in playoff)
    assert not any(c.hard for c in europa + conference)


def test_a_champions_league_loss_drops_into_the_europa_league(frame):
    probabilities = frame.uefa_trees["bodo_glimt_m"].probabilities
    # Q3 lost (0.5) or play-off lost (0.25): the Europa League league phase.
    assert probabilities["bodo_glimt_m/europa_league_main_2026/league"] == 0.75
    assert probabilities["bodo_glimt_m/champions_league_main_2026/league"] == 0.25
    # Viking enter at the play-off; a loss there is the Europa League.
    assert frame.uefa_trees["viking_m"].probabilities["viking_m/europa_league_main_2026/league"] == 0.5


def test_tromso_europa_and_conference_play_offs_are_exclusive(frame):
    tree = frame.uefa_trees["tromso_m"]
    assert "tromso_m/conference_league_2026/playoff" in tree.exclusive_with(
        "tromso_m/europa_league_2026/playoff"
    )
    # The Europa League Q3 comes before both.
    assert tree.exclusive_with("tromso_m/europa_league_2026/q3") == frozenset()


def test_cup_rounds_after_the_entry_round_are_contingent_with_compounding_probability(frame):
    by_node = {c.node_id: c for c in frame.commitments if c.source == "cup"}
    assert by_node["brann_m/cup_men_2027/r1"].reached_probability == 1.0
    assert by_node["brann_m/cup_men_2027/r2"].reached_probability == 0.85
    assert by_node["brann_m/cup_men_2027/r3"].reached_probability == pytest.approx(0.85**2)
    assert by_node["brann_m/cup_men_2027/r4"].reached_probability == pytest.approx(0.85**3)
    assert by_node["brann_m/cup_men_2027/qf"].reached_probability == pytest.approx(0.85**3 * 0.5)
    assert not by_node["brann_m/cup_men_2027/r2"].hard


def test_toppserien_entrants_get_commitments_from_the_womens_european_competitions(frame):
    for team in ("brann_w", "valerenga_w", "rosenborg_w"):
        competitions = {c.competition_id for c in frame.commitments.for_team(team) if c.source == "uefa"}
        assert any(c.startswith("womens_") for c in competitions), team
    assert {
        c.competition_id for c in frame.commitments.for_team("brann_w") if c.source == "uefa"
    } == {"womens_champions_league_2026", "womens_europa_cup_2026"}


def test_womens_europe_is_hard_only_at_entry_and_contingent_after(frame):
    by_node = {c.node_id: c for c in frame.commitments}
    assert by_node["brann_w/womens_champions_league_2026/q2"].hard
    assert by_node["rosenborg_w/womens_europa_cup_2026/q1"].hard
    for node in (
        "brann_w/womens_champions_league_2026/q3",
        "brann_w/womens_europa_cup_2026/q1",
        "valerenga_w/womens_europa_cup_2026/q2",
        "rosenborg_w/womens_europa_cup_2026/q2",
    ):
        assert not by_node[node].hard, node


def test_womens_uefa_weeks_never_touch_the_mens_clubs(frame):
    womens = [w for w in frame.uefa_weeks if w.competition_id.startswith("womens_")]
    assert womens
    for week in womens:
        assert all(t.endswith("_w") for t in week.teams), week.label
