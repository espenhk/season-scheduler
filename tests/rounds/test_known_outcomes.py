"""Known results settle branches of the outcome trees (#13)."""

from __future__ import annotations

import pytest

from terminliste.frame import build_season_frame
from terminliste.rounds.known_outcomes import KnownOutcomeError

PLAYOFF = "brann_m/conference_league_2026/playoff"
Q3 = "brann_m/conference_league_2026/q3"
MAIN = "brann_m/conference_league_main_2026/league"


def _commitments(world, season, outcomes, team="brann_m"):
    frame, _ = build_season_frame(world, season, outcomes)
    return {c.node_id: c for c in frame.commitments.for_team(team)}


def test_won_makes_the_next_round_certain(world, season):
    before = _commitments(world, season, None)
    after = _commitments(world, season, {"brann_m": {"conference_league_2026": {"q3": "won"}}})

    assert not before[PLAYOFF].hard and before[PLAYOFF].reached_probability < 0.5
    assert after[Q3].hard and after[PLAYOFF].hard
    assert after[PLAYOFF].reached_probability == pytest.approx(1.0)


def test_lost_cuts_the_win_branch(world, season):
    after = _commitments(world, season, {"brann_m": {"conference_league_2026": {"q3": "lost"}}})

    assert after[Q3].hard
    assert PLAYOFF not in after
    assert MAIN not in after


def test_reached_makes_earlier_rounds_certain_too(world, season):
    after = _commitments(world, season, {"brann_m": {"conference_league_2026": {"playoff": "reached"}}})

    assert after[Q3].hard and after[PLAYOFF].hard


def test_season_data_and_extra_outcomes_merge(world, season):
    seasoned = season.model_copy(
        update={"known_outcomes": {"brann_m": {"conference_league_2026": {"q3": "won"}}}}
    )
    after = _commitments(world, seasoned, None)

    assert after[PLAYOFF].hard


def test_cup_rounds_can_be_settled(world, season):
    after = _commitments(world, season, {"brann_m": {"cup_men_2027": {"r2": "lost"}}})

    assert "brann_m/cup_men_2027/r3" not in after
    assert after["brann_m/cup_men_2027/r2"].hard


@pytest.mark.parametrize(
    "outcomes",
    [
        {"brann_m": {"conference_league_2026": {"q9": "won"}}},
        {"nobody": {"conference_league_2026": {"q3": "won"}}},
        {"brann_m": {"conference_league_2026": {"q3": "drew"}}},
    ],
)
def test_unknown_entries_are_rejected(world, season, outcomes):
    with pytest.raises(KnownOutcomeError):
        build_season_frame(world, season, outcomes)
