"""Report rendering: fairness aggregation, combined views, and the generated
HTML page's new controls (issues #21-25, #39)."""

from __future__ import annotations

from datetime import date

import factories as f
from terminliste.report.render import _fairness_rows, _match_entries, render_report
from terminliste.scoring.base import ConstraintResult, EvalContext, Event, evaluate
from terminliste.scoring.soft import ConsecutiveHomeDays
from terminliste.solvers.base import Candidate, SolverResult


class _FixedTravel:
    def hours(self, a, b):
        return 1.0


def _two_dual_clubs_world():
    va, vb, vc = f.venue("va"), f.venue("vb"), f.venue("vc")
    a_m = f.team("a_m", "club_a", "va", gender="men")
    a_w = f.team("a_w", "club_a", "va", gender="women")
    b_m = f.team("b_m", "club_b", "vb", gender="men")
    b_w = f.team("b_w", "club_b", "vb", gender="women")
    opp_m = f.team("opp_m", "opp1", "vc", gender="men")
    opp_w = f.team("opp_w", "opp2", "vc", gender="women")
    clubs = [
        f.club("club_a", [a_m, a_w], color="#112233"),
        f.club("club_b", [b_m, b_w], color="#445566"),
        f.club("opp1", [opp_m], color="#778899"),
        f.club("opp2", [opp_w], color="#aabbcc"),
    ]
    comp_m = f.competition("elite", ["a_m", "b_m", "opp_m"], gender="men")
    comp_w = f.competition("topp", ["a_w", "b_w", "opp_w"], gender="women")
    world = f.world(clubs, [va, vb, vc], [comp_m, comp_w])
    season = f.season(competitions=["elite", "topp"])
    return world, season, comp_m, comp_w


def _candidate(world, season, matches, constraints, label="Option 1"):
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=True)
    score = evaluate(matches, constraints, ctx)
    return Candidate(matches=matches, score=score, label=label)


# -- fairness ----------------------------------------------------------------


def test_fairness_flags_a_club_that_never_gets_the_back_to_back_home_reward():
    """Club A pairs up its two teams' home days twice, club B never does —
    exactly the "not zero, and not double" scenario issue #23 asks to catch."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "a_m", "opp_m", date(2026, 7, 4), "va", round_index=1),
        f.match("topp", "a_w", "opp_w", date(2026, 7, 5), "va", round_index=1),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 6), "vb"),
        f.match("topp", "b_w", "opp_w", date(2026, 6, 20), "vb", round_index=1),
    ]
    candidate = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])

    rows = _fairness_rows(world, candidate)
    row = next(r for r in rows if r["id"] == "consecutive_home_days")
    counts = {e["label"]: e["count"] for e in row["entries"]}

    assert counts["Club A"] == 2
    assert counts["Club B"] == 0
    assert row["flagged"] is True


def test_fairness_does_not_flag_a_roughly_even_split():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 6), "vb"),
        f.match("topp", "b_w", "opp_w", date(2026, 6, 7), "vb"),
    ]
    candidate = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])

    rows = _fairness_rows(world, candidate)
    row = next(r for r in rows if r["id"] == "consecutive_home_days")
    counts = {e["label"]: e["count"] for e in row["entries"]}

    assert counts["Club A"] == 1
    assert counts["Club B"] == 1
    assert row["flagged"] is False


def test_fairness_rows_omit_rules_that_were_not_evaluated():
    """A rule with no `ConstraintResult` on the score (not run for this
    candidate) has no per-team counts to show — the row is omitted rather
    than rendered as an all-zero table."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])

    rows = _fairness_rows(world, candidate)
    assert all(r["id"] != "home_away_balance" for r in rows)


def test_fairness_rows_pick_up_a_brand_new_team_scoped_rule_automatically():
    """`_fairness_rows` must not need a per-rule registration list — a fresh
    soft-rule id shows up here purely because its own events carry
    `team_ids`, with no matching change needed in render.py."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 13), "vb", round_index=1),
    ]
    candidate = _candidate(world, season, matches, [])
    candidate.score.results.append(
        ConstraintResult(
            constraint_id="brand_new_soft_rule",
            kind="soft",
            total=3.0,
            count=2,
            events=[
                Event(delta=2.0, detail="a_m favoured", team_ids=("a_m",)),
                Event(delta=1.0, detail="b_m favoured", team_ids=("b_m",)),
            ],
        )
    )

    rows = _fairness_rows(world, candidate)
    row = next(r for r in rows if r["id"] == "brand_new_soft_rule")
    counts = {e["label"]: e["count"] for e in row["entries"]}
    assert counts[world.team_label("a_m")] == 1
    assert counts[world.team_label("b_m")] == 1


def test_fairness_rows_pick_up_a_brand_new_club_coupling_rule_automatically():
    """Same guarantee for the other shape: an event pairing two teams of the
    same club is attributed to that club, purely inferred from the event's
    own `team_ids` — no `if constraint_id == ...` branch to update."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
    ]
    candidate = _candidate(world, season, matches, [])
    candidate.score.results.append(
        ConstraintResult(
            constraint_id="brand_new_club_rule",
            kind="soft",
            total=5.0,
            count=1,
            events=[Event(delta=5.0, detail="club a paired", team_ids=("a_m", "a_w"))],
        )
    )

    rows = _fairness_rows(world, candidate)
    row = next(r for r in rows if r["id"] == "brand_new_club_rule")
    counts = {e["label"]: e["count"] for e in row["entries"]}
    assert counts["Club A"] == 1
    assert counts["Club B"] == 0


# -- combined view -------------------------------------------------------


def test_match_entries_carry_short_names_and_club_ids_for_filtering():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    club_colors = {c.id: c.color for c in world.clubs.values()}

    entries = _match_entries(world, candidate, club_colors, {"club_a", "club_b"})
    assert len(entries) == 1
    entry = entries[0]
    assert entry["home_short"] == world.team_short_label("a_m")
    assert entry["away_short"] == world.team_short_label("opp_m")
    assert entry["home_club"] == "club_a"
    assert entry["away_club"] == "opp1"
    assert entry["color"] == "#112233"
    assert entry["away_color"] == "#778899"


def test_combined_calendar_interleaves_both_competitions_in_the_same_week():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    same_week = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
    ]
    candidate = _candidate(world, season, same_week, [])

    result = SolverResult(candidates=[candidate], solver="test")
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        html = out.read_text(encoding="utf-8")

    assert "Combined · calendar" in html
    assert "Combined · list" in html
    assert 'data-view="combined-list"' in html
    assert 'class="club-select"' in html
    # Both competitions' short names show up, and both matches share one
    # combined week group (there is exactly one "Week" heading between them).
    assert html.count("Week ") >= 1
