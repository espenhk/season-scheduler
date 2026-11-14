"""Known results settle branches of the outcome trees (#13)."""

from __future__ import annotations

import pytest

from terminliste.frame import build_season_frame
from terminliste.rounds.known_outcomes import KnownOutcomeError, merge_outcomes

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


def test_lost_drops_every_commitment_after_it(world, season):
    after = _commitments(world, season, {"brann_m": {"conference_league_2026": {"q3": "lost"}}})

    assert after[Q3].hard
    assert PLAYOFF not in after
    assert MAIN not in after


def test_reached_makes_earlier_rounds_certain_too(world, season):
    after = _commitments(world, season, {"brann_m": {"conference_league_2026": {"playoff": "reached"}}})

    assert after[Q3].hard and after[PLAYOFF].hard


def test_a_lost_cup_round_ends_the_run(world, season):
    after = _commitments(world, season, {"brann_m": {"cup_men_2027": {"r2": "lost"}}})

    assert "brann_m/cup_men_2027/r3" not in after
    assert after["brann_m/cup_men_2027/r2"].hard


def test_no_outcomes_leave_the_frame_unchanged(world, season):
    plain, _ = build_season_frame(world, season)
    same, _ = build_season_frame(world, season, {})

    assert [(c.node_id, c.date, c.hard) for c in plain.commitments] == [
        (c.node_id, c.date, c.hard) for c in same.commitments
    ]


def test_resolved_trees_collapse_the_outcome_paths(world, season):
    tree = build_season_frame(world, season)[0].uefa_trees["viking_m"]
    settled = build_season_frame(
        world, season, {"viking_m": {"champions_league_2026": {"playoff": "lost"}}}
    )[0].uefa_trees["viking_m"]

    assert len(settled.paths()) < len(tree.paths())
    assert sum(p for _, p in settled.paths()) == pytest.approx(1.0)


def test_later_sources_win_when_merged():
    first = {"a": {"cup": {"r1": "won", "r2": "reached"}}}
    second = {"a": {"cup": {"r1": "lost"}}, "b": {"cup": {"r1": "won"}}}

    assert merge_outcomes(first, None, second) == {
        "a": {"cup": {"r1": "lost", "r2": "reached"}},
        "b": {"cup": {"r1": "won"}},
    }


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


def test_winning_retires_the_branch_it_rules_out(world, season):
    from terminliste.replan import settled_nodes

    before = build_season_frame(world, season)[0].commitments
    after = build_season_frame(
        world, season, {"bodo_glimt_m": {"champions_league_2026": {"playoff": "won"}}}
    )[0].commitments
    triggered, retired = settled_nodes(before, after)

    siblings = {c.node_id for c in before if c.node_id.startswith("bodo_glimt_m/europa_league_main_2026/")}
    assert siblings and siblings <= set(retired)
    assert "bodo_glimt_m/champions_league_2026/playoff" in triggered
    assert not set(retired) & {c.node_id for c in after if c.reached_probability > 0}
