"""Report rendering: fairness aggregation, combined views, and the generated
HTML page's new controls (issues #21-25, #39)."""

from __future__ import annotations

import itertools
import json
import math
import re
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import factories as f
import pytest

from terminliste.report import payload
from terminliste.report.payload import json_for_script
from terminliste.report.render import (
    _build_option,
    _club_headlines,
    _comparison_view,
    _competition_colors,
    _cup_entries,
    _day_competitions,
    _entity_counts,
    _european_entries,
    _events,
    _fairness_rows,
    _headlines,
    _match_entries,
    _month_calendar_view,
    _option_summaries,
    _rule_ids,
    _score_marks,
    _search_stats_view,
    _signed,
    _tint,
    render_report,
)
from terminliste.scoring.base import ConstraintResult, EvalContext, Event, Score, evaluate
from terminliste.scoring.hard import MinRestDays, OneMatchPerTeamPerDay
from terminliste.scoring.registry import rule_name
from terminliste.scoring.soft import (
    ConsecutiveHomeDays,
    HomeAwayBreaks,
    PreferredWeekday,
    RestComfort,
)
from terminliste.solvers.base import Candidate, SearchStats, SolverResult


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


def _render(world, season, candidates, **kwargs) -> str:
    result = SolverResult(candidates=candidates, solver="test")
    with tempfile.TemporaryDirectory() as tmp:
        return render_report(world, season, result, Path(tmp) / "out.html", **kwargs).read_text(
            encoding="utf-8"
        )


def _report_data(rendered: str) -> dict:
    """The fixture payload the page's script draws from, parsed back out."""
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', rendered, re.S)
    assert match, "report-data script missing"
    return json.loads(match.group(1))


def _fixtures(data: dict, option: int = 0) -> list[dict]:
    """The option's fixtures indexed by `uid`: shared cup/European entries, then league matches."""
    return data["shared"] + data["options"][option]["matches"]


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


# -- per-team/per-club occurrence counts --------------------------------


def test_entity_counts_picks_the_larger_event_bucket_like_fairness_rows_does():
    """`_entity_counts` must resolve a rule with both event shapes the same
    way `_fairness_row_for` does — the larger bucket wins — so the "Biggest
    upsides"/"Biggest problems" occurrence list never disagrees with the
    fairness table about which entities a rule's events belong to."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    result = ConstraintResult(
        constraint_id="mixed_shape_rule",
        kind="soft",
        total=4.0,
        count=3,
        events=[
            Event(delta=1.0, detail="club a paired", team_ids=("a_m", "a_w")),
            Event(delta=1.0, detail="a_m favoured", team_ids=("a_m",)),
            Event(delta=1.0, detail="b_m favoured", team_ids=("b_m",)),
            Event(delta=1.0, detail="opp_m favoured", team_ids=("opp_m",)),
        ],
    )

    entries = {e["label"]: e["count"] for e in _entity_counts(world, result)}

    # 1 club-coupling event vs. 3 single-team events: team_events wins.
    assert entries == {
        world.team_label("a_m"): 1,
        world.team_label("b_m"): 1,
        world.team_label("opp_m"): 1,
    }


def test_entity_counts_returns_nothing_for_a_rule_with_no_team_ids():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    result = ConstraintResult(
        constraint_id="preferred_weekday",
        kind="soft",
        total=2.0,
        count=1,
        events=[Event(delta=2.0, detail="elite: 1/1 matches on Sunday")],
    )
    assert _entity_counts(world, result) == []


# -- per-club headline stats ----------------------------------------------


def test_club_headlines_restate_matches_weekday_pct_and_pairing_counts_per_club():
    """The numbers shown when the calendar's club filter narrows to one club
    — a smaller, club-scoped echo of the season-wide `_headlines`."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        # Club A: two back-to-back home pairs, half its matches on Sunday.
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),  # Sat
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),  # Sun
        f.match("elite", "a_m", "opp_m", date(2026, 7, 4), "va", round_index=1),  # Sat
        f.match("topp", "a_w", "opp_w", date(2026, 7, 5), "va", round_index=1),  # Sun
        # Club B: no pairing, no Sunday matches.
        f.match("elite", "b_m", "opp_m", date(2026, 6, 6), "vb"),  # Sat
        f.match("topp", "b_w", "opp_w", date(2026, 6, 20), "vb", round_index=1),  # Sat
    ]
    candidate = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])
    dual_club_ids = {"club_a", "club_b"}

    headlines = _club_headlines(world, candidate, dual_club_ids)

    assert headlines["club_a"] == [
        {"value": "4", "label": "matches"},
        {"value": "50%", "label": "on the preferred weekday"},
        {"value": "2", "label": "back-to-back home days"},
        {"value": "0", "label": "paired away days within travel range"},
    ]
    assert headlines["club_b"] == [
        {"value": "2", "label": "matches"},
        {"value": "0%", "label": "on the preferred weekday"},
        {"value": "0", "label": "back-to-back home days"},
        {"value": "0", "label": "paired away days within travel range"},
    ]


def test_club_headlines_omit_pairing_rows_for_a_non_dual_club():
    """A single-team club (not in `dual_club_ids`) only gets the two numbers
    that make sense for it — pairing stats are dual-club-only concepts."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])

    headlines = _club_headlines(world, candidate, dual_club_ids={"club_a"})

    assert headlines["opp1"] == [
        {"value": "1", "label": "matches"},
        {"value": "0%", "label": "on the preferred weekday"},
    ]


# -- script-safe JSON -------------------------------------------------------


def test_json_for_script_cannot_close_or_open_markup_inside_a_script_element():
    """The payload sits inside `<script type="application/json">`, so no data
    string may contain `</script>`, `<!--` or a raw `&`; the escapes are plain
    JSON, so `json.loads` must still recover the original text exactly."""
    payload = {"name": "</script><script>alert(1)</script><!-- & \u2028 \u2029", "n": [1, "é"]}

    text = json_for_script(payload)

    for forbidden in ("<", ">", "&", "\u2028", "\u2029"):
        assert forbidden not in text
    assert json.loads(text) == payload


def test_render_report_embeds_hostile_names_without_breaking_out_of_the_data_script():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    candidate = _candidate(world, season, [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")], [])
    hostile = '</script><script>alert(1)</script><!-- "quoted" & more'
    schedule = f.cup_schedule(
        "cup1",
        [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, round_name=hostile, venue_type="away")],
        competition_name=hostile,
    )

    rendered = _render(world, season, [candidate], cup_schedules=[schedule])

    # Exactly the two script elements the template defines: the data and the renderer.
    assert rendered.count("<script") == 2
    data = _report_data(rendered)
    assert data["shared"][0]["n"] == hostile
    assert data["competitions"]["cup1"]["name"] == hostile


# -- combined view -------------------------------------------------------


def test_match_entries_carry_team_ids_venue_and_competition_for_the_payload():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])

    entries = _match_entries(world, candidate, {"club_a", "club_b"})
    assert len(entries) == 1
    entry = entries[0]
    assert entry["home_team"] == "a_m"
    assert entry["away_team"] == "opp_m"
    assert entry["venue_id"] == "va"
    assert entry["home"] == world.team_label("a_m")
    assert entry["flags"] == []
    assert entry["competition_short"] == comp_m.short_name


def test_combined_calendar_interleaves_both_competitions_in_the_same_week():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    same_week = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
    ]
    candidate = _candidate(world, season, same_week, [])

    rendered = _render(world, season, [candidate])
    data = _report_data(rendered)

    assert "Combined · calendar" in rendered
    assert "Combined · list" in rendered
    assert 'data-view="combined-list"' in rendered
    assert 'class="club-select"' in rendered
    # Both matches share one combined week group, and the list holds both.
    option = data["options"][0]
    assert len(option["weeks"]) == 1
    fixtures = _fixtures(data)
    assert {fixtures[uid]["c"] for uid in option["weeks"][0]["ids"]} == {"elite", "topp"}
    assert sorted(option["list"]) == sorted(option["weeks"][0]["ids"])


def test_cup_entries_flatten_one_row_per_entered_team_per_round():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    competition_colors = {"cup1": {"fg": "#111111", "bg": "#eeeeee"}}
    schedule = f.cup_schedule(
        "cup1",
        [
            f.cup_placement(
                "r1",
                {"a_m": date(2026, 3, 1), "b_m": date(2026, 3, 2)},
                round_name="Round 1",
                venue_type="away",
            )
        ],
        competition_name="NM Cup",
        competition_short_name="NM",
    )

    entries = _cup_entries(world, [schedule], competition_colors)

    assert len(entries) == 2
    entry = next(e for e in entries if e["team_id"] == "a_m")
    assert entry["team"] == world.team_short_label("a_m")
    assert entry["competition_id"] == "cup1"
    assert entry["competition_name"] == "NM Cup"
    assert entry["competition_short"] == "NM"
    assert entry["round_name"] == "Round 1"
    assert entry["venue_type"] == "away"
    assert entry["date"] == date(2026, 3, 1)
    assert entry["comp_fg"] == "#111111"


def test_combined_views_include_cup_rounds_alongside_league_matches():
    """A cup round must show up in the combined calendar and combined list as
    well as under "By competition" — the place a reader checks for "what's
    happening this week" across everything."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    schedule = f.cup_schedule(
        "cup1",
        [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, round_name="Round 1", venue_type="away")],
        competition_name="NM Cup",
    )

    data = _report_data(_render(world, season, [candidate], cup_schedules=[schedule]))

    option = data["options"][0]
    fixtures = _fixtures(data)
    (cup_uid,) = [uid for uid, fx in enumerate(fixtures) if fx.get("k") == "cup"]
    cup = fixtures[cup_uid]
    assert cup["t"] == "a_m" and cup["n"] == "Round 1" and cup["vt"] == "away"
    assert data["competitions"]["cup1"]["name"] == "NM Cup"
    assert cup_uid in option["list"]
    assert any(cup_uid in week["ids"] for week in option["weeks"])
    assert any(cup_uid in day["ids"] for day in option["days"])
    (cup_view,) = data["sharedByCompetition"]
    assert cup_view["name"] == "NM Cup"
    assert [r["ids"] for r in cup_view["rounds"]] == [[cup_uid]]


def test_european_entries_flatten_one_row_per_entered_team_per_leg():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    competition_colors = {"cl": {"fg": "#111111", "bg": "#eeeeee"}}
    round_ = f.european_round(
        "q3",
        first_leg=f.european_leg(forced_date=date(2026, 8, 4)),
        second_leg=f.european_leg(forced_date=date(2026, 8, 11)),
        ties=[f.european_tie("a_m", opponent="Union Saint-Gilloise", home_leg="second")],
    )
    comp = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "q3"): (date(2026, 8, 4), date(2026, 8, 11))}

    entries = _european_entries(world, [comp], resolved_legs, competition_colors)

    assert len(entries) == 2
    first_leg_entry = next(e for e in entries if e["date"] == date(2026, 8, 4))
    assert first_leg_entry["team_id"] == "a_m"
    assert first_leg_entry["competition_id"] == "cl"
    assert first_leg_entry["opponent"] == "Union Saint-Gilloise"
    # home_leg="second" -> the first leg is away for this team.
    assert first_leg_entry["venue_type"] == "away"
    assert first_leg_entry["comp_fg"] == "#111111"
    assert first_leg_entry["competition_short"] == comp.short_name

    second_leg_entry = next(e for e in entries if e["date"] == date(2026, 8, 11))
    assert second_leg_entry["venue_type"] == "home"


def test_european_entries_use_unknown_venue_type_when_home_leg_is_not_set():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    round_ = f.european_round(
        "playoff",
        forced_date=date(2026, 8, 18),
        ties=[f.european_tie("a_m")],  # opponent/home_leg left at their defaults
    )
    comp = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "playoff"): (date(2026, 8, 18), date(2026, 8, 18))}

    entries = _european_entries(world, [comp], resolved_legs, {})
    assert all(e["venue_type"] == "unknown" for e in entries)
    assert all(e["opponent"] == "TBD" for e in entries)


def test_round_legs_collapses_a_shared_date_to_one_entry():
    """`_round_legs` is what stops a same-date round (a main tournament
    matchday, or a final whose second leg was folded onto the first's date)
    from producing a spurious 'first leg'/'second leg' duplicate — a real
    two-legged round (different dates) still produces both."""
    from terminliste.report.render import _round_legs

    assert _round_legs(date(2026, 9, 10), date(2026, 9, 10)) == [(None, date(2026, 9, 10))]
    assert _round_legs(date(2026, 8, 18), date(2026, 8, 25)) == [
        ("first", date(2026, 8, 18)),
        ("second", date(2026, 8, 25)),
    ]


def test_main_tournament_renders_via_the_display_adapter():
    """A main tournament (#79) has no `european_rounds`, so the display
    adapter supplies them and the report's European views render its
    matchdays and knockout legs like a qualifying competition's — without
    splitting a same-date round into two fixture rows."""
    from terminliste.report.render import _european_views
    from terminliste.rounds.european_schedule import (
        build_main_tournament_rounds_for_display,
    )

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
        knockout_rounds=[
            f.main_tournament_round(
                "r16",
                first_leg=f.european_leg(forced_date=date(2027, 3, 10)),
                second_leg=f.european_leg(forced_date=date(2027, 3, 17)),
            ),
            f.main_tournament_round("final", forced_date=date(2027, 6, 5), venue_name="Wembley Stadium"),
        ],
    )

    rounds, resolved_legs = build_main_tournament_rounds_for_display(main, ["a_m"], set())
    display_main = main.model_copy(update={"european_rounds": rounds})

    entries = _european_entries(world, [display_main], resolved_legs, {})
    # One reachable team (a_m): 1 matchday + 2 r16 legs + 1 final = 4 rows —
    # not 6, which is what emitting two rows for the matchday and the final
    # (the bug the review caught) would produce.
    assert len(entries) == 4
    assert sorted(e["date"] for e in entries) == [
        date(2026, 9, 10),
        date(2027, 3, 10),
        date(2027, 3, 17),
        date(2027, 6, 5),
    ]
    assert all(e["team"] == world.team_short_label("a_m") for e in entries)
    assert all(e["opponent"] == "TBD" for e in entries)
    matchday_entry = next(e for e in entries if e["date"] == date(2026, 9, 10))
    assert matchday_entry["round_name"] == "md1"  # no "(None leg)"/"(first leg)" suffix
    final_entry = next(e for e in entries if e["date"] == date(2027, 6, 5))
    assert final_entry["round_name"] == "final"

    views = _european_views([display_main], resolved_legs, entries)
    assert len(views) == 1
    assert "3 matchday(s)/round(s) tracked" in views[0]["meta"]
    matchday_round = next(r for r in views[0]["rounds"] if r["title"] == "md1")
    assert len(matchday_round["fixtures"]) == 1
    r16_round = next(r for r in views[0]["rounds"] if r["title"] == "r16")
    assert len(r16_round["fixtures"]) == 2
    final_round = next(r for r in views[0]["rounds"] if r["title"] == "final")
    assert len(final_round["fixtures"]) == 1
    assert "Wembley Stadium" in final_round["note"]


def test_combined_views_include_european_rounds_alongside_league_matches():
    """A European round must show up under "By competition" and in the
    combined calendar/list views, with the real opponent shown where the data
    has one."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    round_ = f.european_round(
        "q3",
        forced_date=date(2026, 6, 6),
        ties=[f.european_tie("a_m", opponent="Union Saint-Gilloise", home_leg="first")],
    )
    comp = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "q3"): (date(2026, 6, 6), date(2026, 6, 6))}

    data = _report_data(
        _render(
            world, season, [candidate], european_competitions=[comp], resolved_legs=resolved_legs
        )
    )

    option = data["options"][0]
    fixtures = _fixtures(data)
    (eu_uid,) = [uid for uid, fx in enumerate(fixtures) if fx.get("k") == "eu"]
    assert fixtures[eu_uid]["o"] == "Union Saint-Gilloise"
    assert eu_uid in option["list"]
    assert any(eu_uid in week["ids"] for week in option["weeks"])
    (eu_view,) = data["sharedByCompetition"]
    assert [r["ids"] for r in eu_view["rounds"]] == [[eu_uid]]


# -- month calendar (issue #77) -------------------------------------------


def test_tint_lightens_a_hex_color_toward_white():
    assert _tint("#000000", factor=0.5) == "#808080"
    assert _tint("#000000", factor=1.0) == "#ffffff"


def test_day_competitions_lists_each_competition_once_ordered_by_name():
    """Two Eliteserien matches on the same day still show one dot — issue
    #77 asks for "only one dot per league per day"."""
    entries = [
        {"competition_id": "topp", "competition_name": "Toppserien"},
        {"competition_id": "elite", "competition_name": "Eliteserien"},
        {"competition_id": "elite", "competition_name": "Eliteserien"},
    ]
    assert _day_competitions(entries) == ["elite", "topp"]


def test_every_payload_fixture_resolves_to_a_club_for_the_club_filter():
    """The page's club filter reads a fixture's clubs off its team ids: both
    sides of a league match, the one entered team of a cup or European entry."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    candidate = _candidate(world, season, [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")], [])
    schedule = f.cup_schedule(
        "cup1", [f.cup_placement("r1", {"b_m": date(2026, 6, 6)}, venue_type="home")]
    )
    round_ = f.european_round(
        "q3", forced_date=date(2026, 6, 6), ties=[f.european_tie("b_w", opponent="X")]
    )
    comp = f.competition("cl", ["b_w"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "q3"): (date(2026, 6, 6), date(2026, 6, 6))}

    data = _report_data(
        _render(
            world,
            season,
            [candidate],
            cup_schedules=[schedule],
            european_competitions=[comp],
            resolved_legs=resolved_legs,
        )
    )

    teams = data["teams"]
    clubs_of = []
    for fx in _fixtures(data):
        teams_in = [fx["h"], fx["a"]] if "h" in fx else [fx["t"]]
        clubs_of.append({teams[t]["club"] for t in teams_in})
    assert sorted(map(sorted, clubs_of)) == [["club_a", "opp1"], ["club_b"], ["club_b"]]
    assert all(club in data["clubs"] for club in {"club_a", "club_b", "opp1"})


def test_month_calendar_view_builds_a_full_month_grid_and_day_details():
    entries = [
        {
            "date": date(2026, 3, 1),  # a Sunday
            "competition_id": "elite",
            "competition_name": "Eliteserien",
            "comp_fg": "#ca6f1e",
            "home": "A",
            "away": "B",
            "home_club": "club_a",
            "away_club": "club_b",
        },
    ]
    months, day_details = _month_calendar_view(entries)

    assert len(months) == 1
    month = months[0]
    assert month["label"] == "March 2026"
    assert all(len(week) == 7 for week in month["weeks"])  # Monday-first, full rows
    assert [iso for week in month["weeks"] for iso in week].count("2026-03-01") == 1
    assert month["key"] == "2026-03"

    assert len(day_details) == 1
    assert day_details[0]["date_iso"] == "2026-03-01"
    assert day_details[0]["competition_ids"] == ["elite"]
    assert day_details[0]["matches"] == entries


def test_month_calendar_view_spans_multiple_months_and_pads_out_month_days():
    entries = [
        {"date": date(2026, 3, 31), "competition_id": "elite", "competition_name": "Eliteserien",
         "comp_fg": "#ca6f1e", "home": "A", "away": "B", "home_club": "club_a", "away_club": "club_b"},
        {"date": date(2026, 4, 1), "competition_id": "topp", "competition_name": "Toppserien",
         "comp_fg": "#8e44ad", "home": "C", "away": "D", "home_club": "club_c", "away_club": "club_d"},
    ]
    months, day_details = _month_calendar_view(entries)

    assert [m["label"] for m in months] == ["March 2026", "April 2026"]
    assert len(day_details) == 2
    # 1 April is padding at the tail of March's grid, but it is an empty cell
    # there: the date is only listed in its own month.
    march = [iso for week in months[0]["weeks"] for iso in week]
    assert march.count("2026-04-01") == 0
    assert march.count(None) == len(march) - 31  # March has 31 days; the rest pad
    april = [iso for week in months[1]["weeks"] for iso in week]
    assert april.count("2026-04-01") == 1


def test_month_calendar_view_is_empty_for_no_entries():
    assert _month_calendar_view([]) == ([], [])


def test_month_calendar_view_sorts_a_day_detail_by_kickoff_time():
    """Tapping a day should show its games earliest kickoff first, not
    grouped by competition name — a league match's `kickoff_time` is the
    sort key; an entry with none (a cup/European entry, or a league match
    not yet assigned one) sorts after every timed entry."""
    entries = [
        {
            "date": date(2026, 3, 1), "competition_id": "topp", "competition_name": "Toppserien",
            "comp_fg": "#8e44ad", "home": "C", "away": "D", "home_club": "club_c", "away_club": "club_d",
            "kickoff_time": "18:00",
        },
        {
            "date": date(2026, 3, 1), "competition_id": "elite", "competition_name": "Eliteserien",
            "comp_fg": "#ca6f1e", "home": "A", "away": "B", "home_club": "club_a", "away_club": "club_b",
            "kickoff_time": "14:00",
        },
        {
            "date": date(2026, 3, 1), "competition_id": "cup", "competition_name": "NM Cup",
            "comp_fg": "#c0392b", "club_id": "club_e", "team": "E",
        },
    ]
    _, day_details = _month_calendar_view(entries)
    assert [m["home"] for m in day_details[0]["matches"][:2]] == ["A", "C"]
    assert day_details[0]["matches"][2]["competition_id"] == "cup"


def test_render_report_month_calendar_has_a_pane_and_carries_days_comps_and_months_as_data():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])

    rendered = _render(world, season, [candidate])
    data = _report_data(rendered)

    assert "Month calendar" in rendered
    assert 'data-view="month-calendar"' in rendered
    option = data["options"][0]
    (month,) = option["months"]
    assert month["label"] == "June 2026" and month["key"] == "2026-06"
    assert all(len(week) == 7 for week in month["weeks"])
    assert "2026-06-06" in [iso for week in month["weeks"] for iso in week]
    (day,) = option["days"]
    assert day["date"] == "2026-06-06"
    assert day["label"] == "Saturday 6 June 2026"
    assert day["comps"] == ["elite"]
    assert [_fixtures(data)[uid]["h"] for uid in day["ids"]] == ["a_m"]


def test_each_options_month_legend_names_only_the_competitions_it_shows():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])

    rendered = _render(world, season, [candidate])

    calendar_pane = rendered.split('class="cal-wrap view-pane" data-view="month-calendar"')[1].split(
        '<div class="pane-body">'
    )[0]
    assert f"<b>{comp_m.short_name}</b>" in calendar_pane
    assert f"<b>{comp_w.short_name}</b>" not in calendar_pane  # no women's date in this option


def test_render_report_month_calendar_legend_and_colours_come_from_css_classes():
    """Colours are custom properties on per-club and per-competition classes,
    not inline styles repeated per element; the legend names each competition
    next to a dot of its class (factories.competition() sets `name=id`)."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])

    rendered = _render(world, season, [candidate])

    calendar_pane = rendered.split('class="cal-wrap view-pane" data-view="month-calendar"')[1].split(
        '<div class="pane-body">'
    )[0]
    assert "elite" in calendar_pane
    assert '<span class="cal-dot c-elite"' in calendar_pane
    assert ".k-club_a{--club:#112233}" in rendered
    assert ".c-elite{--comp-fg:" in rendered
    assert 'style="background' not in rendered
    data = _report_data(rendered)
    assert data["clubs"]["club_a"] == "k-club_a"
    assert data["competitions"]["elite"]["cls"] == "c-elite"
    assert data["competitions"]["elite"]["short"] == comp_m.short_name
    assert data["competitions"]["elite"]["name"] == comp_m.name


def _srgb_channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_colour: str) -> float:
    r, g, b = (_srgb_channel(int(hex_colour[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    light, dark = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _lab(hex_colour: str) -> tuple[float, float, float]:
    r, g, b = (_srgb_channel(int(hex_colour[i : i + 2], 16)) for i in (1, 3, 5))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _ciede2000(a: str, b: str) -> float:
    """CIEDE2000 colour difference (Sharma, Wu and Dalal, 2005); about 2.3 is a just-noticeable step."""
    l1, a1, b1 = _lab(a)
    l2, a2, b2 = _lab(b)
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dl, dc = l2 - l1, c2p - c1p
    if c1p * c2p == 0:
        dh = 0.0
    else:
        dh = h2p - h1p
        if dh > 180:
            dh -= 360
        elif dh < -180:
            dh += 360
    d_big_h = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    l_bar, c_bar_p = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        h_bar = (h1p + h2p + 360) / 2
    else:
        h_bar = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_bar - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar))
        + 0.32 * math.cos(math.radians(3 * h_bar + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar - 63))
    )
    d_theta = 30 * math.exp(-(((h_bar - 275) / 25) ** 2))
    rc = 2 * math.sqrt(c_bar_p**7 / (c_bar_p**7 + 25**7))
    sl = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    sc = 1 + 0.045 * c_bar_p
    sh = 1 + 0.015 * c_bar_p * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt(
        (dl / sl) ** 2 + (dc / sc) ** 2 + (d_big_h / sh) ** 2 + rt * (dc / sc) * (d_big_h / sh)
    )


def _hue(hex_colour: str) -> float:
    _, a, b = _lab(hex_colour)
    return math.degrees(math.atan2(b, a)) % 360


def _colour_family(competition) -> str:
    """Eliteserien and Toppserien are each a family of their own; the cups share one, and the UEFA competitions another."""
    if competition.format == "league":
        return competition.id
    return "cup" if competition.format == "cup" else "uefa"


def test_competition_colours_form_four_families_that_read_apart_in_the_real_data(world):
    """The month calendar tells competitions apart by a dot and, on a phone,
    only a count, so the colours must be clearly distinct and must group the
    competitions into four families: Eliteserien and Toppserien each their own,
    the NM cups one red family, the UEFA competitions one green-olive family.

    Checked on the real data: families at least 25 CIEDE2000 apart (the shipped
    minimum is 25.6); every pair within a family at least 12 apart (shipped 16.8
    for UEFA, 18.6 for NM); each UEFA qualifier at least 10 from its own main
    tournament and 15 L* lighter than it; family hue spans within 90 degrees;
    every colour at L* 25 or more, so none reads as near-black; no competition
    colour repeats a club's. A dot reads at 3:1 on white, and the tag and label
    text (the ink, see payload.ink_for) at 4.5:1 on its tint."""
    colours = {c.id: c.color for c in world.competitions.values()}
    families: dict[str, list] = {}
    for competition in world.competitions.values():
        families.setdefault(_colour_family(competition), []).append(competition)
    assert len(families) == 4

    cross = [
        _ciede2000(colours[a.id], colours[b.id])
        for fa, fb in itertools.combinations(families, 2)
        for a in families[fa]
        for b in families[fb]
    ]
    # A readability floor, not a target: the shipped minimum sits just above it, so rebalance
    # the colours rather than lowering it.
    assert min(cross) >= 25

    for members in families.values():
        for a, b in itertools.combinations(members, 2):
            assert _ciede2000(colours[a.id], colours[b.id]) >= 12, (a.id, b.id)
        hues = [_hue(colours[m.id]) for m in members]
        assert max(hues) - min(hues) <= 90, [m.id for m in members]

    mains = {c.name: c for c in families["uefa"] if "qualifying" not in c.name.lower()}
    qualifiers = [c for c in families["uefa"] if "qualifying" in c.name.lower()]
    assert len(qualifiers) == len(mains) == 3
    for qualifier in qualifiers:
        main = mains[qualifier.name.replace(" qualifying", "")]
        assert _ciede2000(colours[main.id], colours[qualifier.id]) >= 10, qualifier.id
        assert _lab(colours[qualifier.id])[0] - _lab(colours[main.id])[0] >= 15, qualifier.id

    assert min(_lab(c)[0] for c in colours.values()) >= 25

    club_colours = {club.color.lower() for club in world.clubs.values() if club.color}
    assert not club_colours & {colour.lower() for colour in colours.values()}

    for competition_id, colour in colours.items():
        assert _contrast(colour, "#ffffff") >= 3, competition_id
        ink = payload.ink_for(colour, _tint(colour))
        assert _contrast(ink, _tint(colour)) >= 4.5, competition_id


# -- embedded fixture payload (#42) ------------------------------------------


def test_each_option_embeds_its_league_matches_once_and_shares_cup_entries():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    first = _candidate(
        world, season, [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")], [], label="Option 1"
    )
    second = _candidate(
        world,
        season,
        [
            f.match("elite", "a_m", "opp_m", date(2026, 6, 13), "va"),
            f.match("topp", "a_w", "opp_w", date(2026, 6, 14), "va"),
        ],
        [],
        label="Option 2",
    )
    schedule = f.cup_schedule(
        "cup1", [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, venue_type="home")]
    )

    rendered = _render(world, season, [first, second], cup_schedules=[schedule])
    data = _report_data(rendered)

    assert len(data["shared"]) == 1  # the cup entry, not one per option
    assert [len(o["matches"]) for o in data["options"]] == [1, 2]
    # Fixtures are drawn by the page's script, not emitted as markup.
    assert 'class="fx ' not in rendered and 'class="fx"' not in rendered
    assert '<tr class="fx-row' not in rendered and 'class="day-row' not in rendered
    # Every uid a view refers to exists in that option's fixture list.
    for index, option in enumerate(data["options"]):
        size = len(_fixtures(data, index))
        referenced = set(option["list"])
        for week in option["weeks"]:
            referenced |= set(week["ids"])
        for day in option["days"]:
            referenced |= set(day["ids"])
        for view in option["byCompetition"]:
            for round_ in view["rounds"]:
                referenced |= set(round_["ids"])
        assert referenced and max(referenced) < size
        # The combined list holds every fixture exactly once.
        assert sorted(option["list"]) == list(range(size))


def test_league_match_wire_is_compact_and_omits_the_defaults():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va", kickoff_time="18:00"),
        f.match("elite", "opp_m", "b_m", date(2026, 6, 7), "va"),  # not opp_m's own ground
    ]
    candidate = _candidate(world, season, matches, [])

    data = _report_data(_render(world, season, [candidate]))

    first, second = data["options"][0]["matches"]
    assert first == {"d": "2026-06-06", "t": "18:00", "c": "elite", "h": "a_m", "a": "opp_m"}
    assert second["v"] == "va" and "t" not in second
    assert data["venues"]["va"] == world.venue("va").name
    assert data["teams"]["a_m"]["short"] == world.team_short_label("a_m")


def _flags_by_home_team(data, option=0):
    """`{home team: [(rule id, delta)]}` for the drawn badges of one option."""
    rules = data["rules"]
    return {
        m["h"]: [(rules[flag[0]]["id"], flag[1]) for flag in m.get("f", [])]
        for m in data["options"][option]["matches"]
    }


def test_paired_home_days_carry_a_home_pair_flag_each():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 7, 4), "vb", round_index=1),
    ]
    constraint = ConsecutiveHomeDays(competitions=[comp_m, comp_w])
    candidate = _candidate(world, season, matches, [constraint])

    data = _report_data(_render(world, season, [candidate]))

    # The pair's reward is split between its two fixtures.
    share = constraint.weight / 2
    assert _flags_by_home_team(data) == {
        "a_m": [("consecutive_home_days", share)],
        "a_w": [("consecutive_home_days", share)],
        "b_m": [],
    }
    flag = data["options"][0]["matches"][0]["f"][0]
    assert flag[3] == 2  # fixtures sharing the event
    assert "home" in data["options"][0]["notes"][flag[2]]
    assert data["rules"][flag[0]]["label"] == "home pair"


def test_preferred_weekday_badges_the_off_day_minority_and_keeps_the_reward_quiet():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    sunday = date(2026, 6, 7)
    matches = [
        f.match("elite", "a_m", "opp_m", sunday, "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 9), "vb", round_index=1),
    ]
    candidate = _candidate(world, season, matches, [PreferredWeekday(competitions=[comp_m, comp_w])])

    data = _report_data(_render(world, season, [candidate]))
    on_day, off_day = data["options"][0]["matches"]

    assert "f" not in on_day
    (rule, delta) = on_day["q"][0]
    assert data["rules"][rule]["id"] == "preferred_weekday" and delta > 0
    ((rule, delta, note, shared, group),) = off_day["f"]
    assert data["rules"][rule]["label"] == "off-day" and delta == 0
    assert shared == 1 and data["options"][0]["groups"][group] == "elite"  # listed by competition
    assert "Tuesday" in data["options"][0]["notes"][note]
    assert "(misses +" in data["options"][0]["notes"][note]
    assert data["tint"] == {"good": 5.0, "cost": -3.0}
    assert "q" not in off_day


def test_hard_rule_events_are_flagged_on_their_fixtures_and_marked_hard():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("elite", "b_m", "a_m", date(2026, 6, 7), "vb", round_index=1),
    ]
    candidate = _candidate(world, season, matches, [MinRestDays(competitions=[comp_m])])

    data = _report_data(_render(world, season, [candidate]))

    first, second = data["options"][0]["matches"]
    assert "f" not in first
    rule = data["rules"][second["f"][0][0]]
    assert rule["id"] == "min_rest_days" and rule["hard"] == 1


def test_every_scoring_rule_has_a_badge_label_and_a_description():
    from terminliste.scoring.registry import BADGE_LABELS, CONSTRAINT_DESCRIPTIONS

    assert set(BADGE_LABELS) == set(CONSTRAINT_DESCRIPTIONS)


def test_every_scoring_rule_has_a_human_name_that_is_not_its_id():
    from terminliste.scoring.registry import CONSTRAINT_DESCRIPTIONS, RULE_NAMES, rule_name

    assert set(RULE_NAMES) == set(CONSTRAINT_DESCRIPTIONS)
    for rule_id, name in RULE_NAMES.items():
        assert name and "_" not in name, rule_id
    assert rule_name("rest_comfort") == "Comfortable rest"
    assert rule_name("not_a_rule") == "not a rule"


def test_result_rows_carry_human_names_and_each_soft_rules_share_of_the_movement():
    from terminliste.report.render import _gross_movement, _result_row, _scoring_view

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    results = [
        ConstraintResult(constraint_id="consecutive_home_days", kind="soft", total=30.0, count=2),
        ConstraintResult(constraint_id="rest_comfort", kind="soft", total=-10.0, count=1),
        ConstraintResult(constraint_id="min_rest_days", kind="hard", total=0.0, count=0),
    ]
    score = Score(hard_violations=0, soft_total=20.0, results=results, num_matches=4)
    gross = _gross_movement(score)
    assert gross == 40.0

    rows = [_result_row(r, world, {}, gross=gross) for r in results]
    assert [row["name"] for row in rows] == ["Back-to-back home days", "Comfortable rest", "Minimum rest between matches"]
    assert rows[0]["share"] == pytest.approx(75.0) and rows[1]["share"] == pytest.approx(25.0)
    assert rows[2]["share"] is None  # hard rules are counted, not weighed
    assert sum(row["share"] for row in rows if row["share"] is not None) == pytest.approx(100.0)

    view = _scoring_view(score, gross)
    assert view["points"] == pytest.approx(score.points)
    assert view["soft_per_match"] == pytest.approx(5.0)
    assert view["saturation"] == pytest.approx(score.soft_saturation)
    assert view["gross"] == gross


def test_the_score_explanation_numbers_come_from_the_same_score_the_page_shows():
    from terminliste.report.render import _gross_movement, _scoring_view
    from terminliste.scoring.base import INFEASIBLE_CEILING, SOFT_SCALE

    feasible = Score(hard_violations=0, soft_total=799.5, results=[], num_matches=96)
    view = _scoring_view(feasible, _gross_movement(feasible))
    assert view["raw_soft"] == 799.5 and view["num_matches"] == 96
    assert view["soft_scale"] == SOFT_SCALE and view["ceiling"] == INFEASIBLE_CEILING
    assert view["points"] == pytest.approx(INFEASIBLE_CEILING + 80 * view["saturation"])
    # Zero soft points sits at the midpoint of the feasible range.
    assert Score(hard_violations=0, soft_total=0.0, num_matches=96).points == pytest.approx(60.0)

    broken = Score(hard_violations=3, soft_total=500.0, results=[], num_matches=96)
    assert broken.points == pytest.approx(INFEASIBLE_CEILING * 2.718281828459045 ** (-0.25 * 3), rel=1e-6)


def _check_flags_sum_to_rule_totals(world, season, candidate):
    """The acceptance rule of #29: a rule's per-fixture deltas, as shipped on
    the wire, add up to its total in the score breakdown. Events pinned to no
    fixture are the only thing left out, and are added back explicitly."""
    data = _report_data(_render(world, season, [candidate]))
    option = data["options"][0]
    sums, counts = Counter(), Counter()
    for m in option["matches"]:
        for flag in m.get("f", []) + m.get("q", []):
            rule_id = data["rules"][flag[0]]["id"]
            sums[rule_id] += flag[1]
            counts[rule_id] += 1

    unpinned = Counter()
    for result in candidate.score.results:
        unpinned[result.constraint_id] += sum(e.delta for e in result.events if not e.match_keys)

    for result in candidate.score.results:
        expected = result.total - unpinned[result.constraint_id]
        tolerance = 0.005 * counts[result.constraint_id] + 1e-6  # deltas are sent to 2 decimals
        assert sums[result.constraint_id] == pytest.approx(expected, abs=tolerance), result.constraint_id
    return unpinned


def test_per_fixture_flag_deltas_add_up_to_each_rules_total():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "a_m", "b_m", date(2026, 6, 9), "va", round_index=1),
        f.match("elite", "opp_m", "a_m", date(2026, 6, 20), "vc", round_index=2),
        f.match("elite", "a_m", "opp_m", date(2026, 6, 24), "va", round_index=3),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 14), "vb", round_index=1),
    ]
    comps = [comp_m, comp_w]
    constraints = [
        PreferredWeekday(competitions=comps),
        ConsecutiveHomeDays(competitions=comps),
        RestComfort(competitions=comps),
        HomeAwayBreaks(competitions=comps),
        MinRestDays(competitions=comps),
    ]
    candidate = _candidate(world, season, matches, constraints)
    totals = {r.constraint_id: r.total for r in candidate.score.results}
    assert totals["rest_comfort"] < 0 and totals["consecutive_home_days"] > 0  # the test bites

    _check_flags_sum_to_rule_totals(world, season, candidate)


def test_flag_deltas_add_up_on_the_real_2026_world(world, season, competitions, travel):
    from terminliste.scoring.registry import build_constraints
    from terminliste.solvers.greedy import build_initial_schedule

    matches, _, _ = build_initial_schedule(world, season, competitions, seed=42)
    ctx = EvalContext(world=world, season=season, travel=travel, detail=True)
    score = evaluate(matches, build_constraints(world, season, competitions), ctx)
    candidate = Candidate(matches=matches, score=score, label="Option 1")

    unpinned = _check_flags_sum_to_rule_totals(world, season, candidate)

    # Only these rules have events that are about no fixture in particular.
    assert {rule for rule, delta in unpinned.items() if delta} <= {
        "home_away_balance",  # half-season drift, no culprit fixture
        "fixed_requirement",  # the required home fixture is missing
        "full_round_on_date",  # teams with no match that day
    }


def test_club_filter_headlines_are_embedded_per_option_for_the_page_to_swap():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    candidate = _candidate(world, season, [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")], [])

    option = _report_data(_render(world, season, [candidate]))["options"][0]

    assert option["clubHeadlines"]["club_a"][0] == {"value": "1", "label": "matches"}
    assert isinstance(option["headlines"], list)


# -- search stats (issue #34) -------------------------------------------------


def test_search_stats_view_is_none_when_nothing_was_tracked():
    """`score`'s imported-schedule path never runs a search — its `SolverResult`
    carries a default, empty `SearchStats`, and the report should just omit
    the section rather than show an all-zero one."""
    assert _search_stats_view(SearchStats()) is None


def test_search_stats_view_summarises_investigated_and_breakdown():
    stats = SearchStats(
        investigated=100,
        feasible=40,
        hard_violation_scenarios=60,
        hard_violation_counts=Counter({"min_rest_days": 50, "venue_double_booking": 10}),
    )
    view = _search_stats_view(stats)

    assert view["investigated"] == 100
    assert view["feasible"] == 40
    assert view["feasible_pct"] == 40.0
    assert view["hard_violation_scenarios"] == 60
    assert view["hard_violation_pct"] == 60.0
    assert view["difficulty"] == "moderate"
    assert view["breakdown"][0]["id"] == "min_rest_days"
    assert view["breakdown"][0]["count"] == 50
    assert view["breakdown"][0]["pct"] == pytest.approx(83.333, rel=1e-3)


def test_render_report_shows_search_difficulty_section_when_stats_present():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    stats = SearchStats(investigated=10, feasible=1, hard_violation_scenarios=9,
                         hard_violation_counts=Counter({"min_rest_days": 9}))
    result = SolverResult(candidates=[candidate], solver="test", iterations=10, search_stats=stats)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "How hard was this to schedule?" in rendered
    assert "difficulty-hard" in rendered
    assert "Minimum rest between matches" in rendered
    assert "min_rest_days" not in rendered.split("<script")[0]


def test_render_report_omits_search_difficulty_section_when_no_stats():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "How hard was this to schedule?" not in rendered


def test_render_report_shows_season_exclusions_with_their_reasons():
    from terminliste.model.schema import DatedNote, GlobalBlackoutRange

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    season = f.season(
        competitions=["elite", "topp"],
        global_blackouts=[DatedNote(date=date(2026, 5, 17), reason="national day")],
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 6, 1), end=date(2026, 6, 14), reason="international break"
            )
        ],
    )
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 3, 1), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "Season exclusions" in rendered
    assert "national day" in rendered
    assert "international break" in rendered
    assert "01 Jun 2026" in rendered and "14 Jun 2026" in rendered


def test_render_report_omits_season_exclusions_section_when_none_configured():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 3, 1), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "Season exclusions" not in rendered


def test_render_report_shows_venue_scoped_exclusions_with_the_venue_name():
    from terminliste.model.schema import VenueBlackout, VenueBlackoutRange

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    season = f.season(
        competitions=["elite", "topp"],
        venue_blackouts=[VenueBlackout(venue="va", date=date(2026, 6, 13), reason="Concert")],
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="vb", start=date(2026, 7, 1), end=date(2026, 7, 3), reason="Ground works"
            )
        ],
    )
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 3, 1), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "Season exclusions" in rendered
    assert "(Va)" in rendered and "Concert" in rendered
    assert "(Vb)" in rendered and "Ground works" in rendered


def test_render_report_sorts_season_exclusions_by_start_date_across_all_four_sources():
    """Regression: the section used to render global dates, then global
    ranges, then (once venue entries existed) venue dates, then venue
    ranges, as four independently-sorted blocks — so a range starting in
    March would land below a single date in December. Building one list and
    sorting it once (see `_season_exclusions`) fixes that."""
    from terminliste.model.schema import DatedNote, GlobalBlackoutRange

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    season = f.season(
        competitions=["elite", "topp"],
        global_blackouts=[DatedNote(date=date(2026, 12, 24), reason="Christmas Eve")],
        global_blackout_ranges=[
            GlobalBlackoutRange(start=date(2026, 3, 1), end=date(2026, 3, 3), reason="March break")
        ],
    )
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 4, 1), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert rendered.index("March break") < rendered.index("Christmas Eve")


def test_render_report_lists_the_round_slots_and_search_space():
    from terminliste.model.calendar import build_calendar
    from terminliste.slot_planner import plan_slots

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    season = f.season(
        competitions=["elite", "topp"], start=date(2026, 3, 1), end=date(2026, 6, 30)
    )
    plans = plan_slots(world, season, [comp_m, comp_w], build_calendar(world, season))
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test", slot_plans=plans)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        rendered = render_report(world, season, result, Path(tmp) / "out.html").read_text(
            encoding="utf-8"
        )

    assert "Round slots" in rendered
    assert f"{comp_m.rounds} rounds in {len(plans['elite'].candidates)} candidate slots" in rendered
    assert "ways to pick compatible slots" in rendered
    first = plans["elite"].chosen[0]
    assert f"default {first.default_day.strftime('%a %d %b')}" in rendered


# -- rule lens (#30) ---------------------------------------------------------


class _Unpinned:
    """A soft rule whose one event points at no fixture, like `home_away_balance`."""

    id = "home_away_balance"
    kind = "soft"
    weight = 1.0

    def evaluate(self, index, ctx):
        return ConstraintResult(
            "home_away_balance", "soft", -6.0, 1, [Event(delta=-6.0, detail="drifted", team_ids=("a_m",))]
        )


def _lens_page():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),  # Saturday, off the preferred Sunday
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 14), "vb", round_index=1),  # Sunday, on day
    ]
    comps = [comp_m, comp_w]
    candidate = _candidate(
        world,
        season,
        matches,
        [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps), _Unpinned()],
    )
    rendered = _render(world, season, [candidate])
    return rendered, _report_data(rendered)


def test_flags_name_the_team_or_club_their_event_is_about_for_the_fixture_lists():
    _, data = _lens_page()
    option = data["options"][0]
    pair, other = option["matches"][0], option["matches"][1]
    flag = next(fl for fl in pair["f"] if data["rules"][fl[0]]["id"] == "consecutive_home_days")
    assert flag[3] == 2  # padded so the group sits in a fixed position
    assert option["groups"][flag[4]] == "Club A"
    assert option["groups"][next(fl for fl in other["f"])[4]] == "Club A"
    assert option["groups"][0] == ""


def test_option_totals_give_each_drawn_rules_full_total_and_mark_the_inverse_rule():
    _, data = _lens_page()
    ids = {i: rule["id"] for i, rule in enumerate(data["rules"])}
    totals = {ids[rule]: total for rule, total in data["options"][0]["totals"]}
    assert set(totals) == {"preferred_weekday", "consecutive_home_days"}  # not the unpinned rule
    assert totals["preferred_weekday"] > 0  # earned by the on-day fixtures, badged on the off-day one
    inverse = [r["id"] for r in data["rules"] if r.get("inv")]
    assert inverse == ["preferred_weekday"]


def test_scorecard_offers_show_in_calendar_only_for_rules_with_fixtures():
    rendered, _ = _lens_page()
    linked = set(re.findall(r'class="lens-link" data-rule="(\w+)"', rendered))
    assert linked == {"preferred_weekday", "consecutive_home_days"}
    assert 'data-rule="home_away_balance"' not in rendered
    assert "Not tied to one fixture" in rendered  # the unpinned rule says why
    # The lazily filled list is announced with its fixture count.
    assert re.search(r'data-rule="consecutive_home_days"><summary>2 fixtures, by team', rendered)


def test_scorecard_keeps_server_text_only_for_events_without_a_fixture():
    rendered, _ = _lens_page()
    markup = rendered.split('<script type="application/json"')[0]
    assert "drifted" in markup  # the unpinned rule's event stays spelled out
    assert "home 2026-06-06" not in markup  # pinned events are listed by the page instead


def test_lens_controls_and_chips_are_in_the_bar_and_the_caption_follows_it():
    rendered, _ = _lens_page()
    bar = rendered[rendered.index('class="controls"') :]
    bar, after = bar[: bar.index('class="lens-caption"')], bar[bar.index('class="lens-caption"') :]
    for hook in ("lens-select", "lens-only-box", "club-select", "club-clear", "lens-clear"):
        assert hook in bar
    assert "lens-clear" not in after[: after.index("</section>")]


def test_each_option_panel_has_one_sticky_bar_with_summary_legend_and_unique_ids():
    rendered, _ = _lens_page()
    assert rendered.count('class="controls"') == rendered.count('role="tabpanel"')
    bar = rendered[rendered.index('class="controls"') :]
    bar = bar[: bar.index('class="lens-caption"')]
    for hook in ("bar-summary", "back-to-score", "legend-toggle", "legend-panel", "filters-toggle", "club-clear", "lens-clear"):
        assert hook in bar
    assert "jump-cal" in rendered
    ids = re.findall(r' id="((?:legend|filters)-\d+)"', rendered)
    assert len(ids) == len(set(ids))


# -- club scope and timeline (issue #33) ---------------------------------------


def test_the_payload_names_the_dual_clubs_and_orders_the_breakdown_for_the_club_scorecard():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    constraints = [PreferredWeekday(competitions=[comp_m, comp_w]), ConsecutiveHomeDays(competitions=[comp_m, comp_w])]
    candidate = _candidate(world, season, matches, constraints)

    data = _report_data(_render(world, season, [candidate]))
    option = data["options"][0]

    assert set(data["dualClubs"]) == {"club_a", "club_b"}
    assert data["clubNames"]["club_a"] == "Club A"
    assert sorted(option["order"]) == sorted(
        data["rules"].index(next(r for r in data["rules"] if r["id"] == res.constraint_id))
        for res in candidate.score.results
    )
    assert option["limit"] == 4 and option["full"] is False


def test_club_unpinned_attributes_season_events_to_the_club_they_name_and_skips_pinned_ones():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    candidate.score.results.append(
        ConstraintResult(
            constraint_id="season_rule",
            kind="soft",
            total=6.0,
            count=3,
            events=[
                Event(delta=3.0, detail="a_m season", team_ids=("a_m",)),
                Event(delta=1.0, detail="a_w season", team_ids=("a_w",)),
                Event(delta=2.0, detail="b_m season", team_ids=("b_m",)),
                Event(delta=9.0, detail="pinned", team_ids=("a_m",), match_keys=(matches[0].key,)),
            ],
        )
    )

    data = _report_data(_render(world, season, [candidate]))
    option = data["options"][0]
    rule = next(i for i, r in enumerate(data["rules"]) if r["id"] == "season_rule")

    # Club A's two teams' events add up; the pinned one is on a fixture already,
    # and Club B's own event goes to Club B.
    assert [row for row in option["clubUnpinned"]["club_a"] if row[0] == rule] == [[rule, 4.0, 2]]
    assert [row for row in option["clubUnpinned"]["club_b"] if row[0] == rule] == [[rule, 2.0, 1]]


def test_club_ranks_read_each_clubs_place_off_the_fairness_rows_with_ties_sharing_a_rank():
    from terminliste.report.render import _club_ranks, _fairness_row

    row = _fairness_row(
        "rest_comfort",
        [("Club A men", 5, "club_a"), ("Club B men", 3, "club_b"), ("Club A women", 3, "club_a"), ("Club C", 1, "club_c")],
        by="team",
    )
    ranks = _club_ranks([row])

    assert ranks["club_a"] == [
        {"id": "rest_comfort", "by": "team", "of": 4, "found": [["Club A men", 5, 1], ["Club A women", 3, 2]]}
    ]
    assert ranks["club_b"][0]["found"] == [["Club B men", 3, 2]]
    assert ranks["club_c"][0]["found"] == [["Club C", 1, 4]]


def test_the_payload_carries_each_teams_gender_and_the_club_ranks_for_the_scorecard():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 13), "vb", round_index=1),
    ]
    constraints = [ConsecutiveHomeDays(competitions=[comp_m, comp_w])]
    candidate = _candidate(world, season, matches, constraints)

    data = _report_data(_render(world, season, [candidate]))
    option = data["options"][0]

    assert data["teams"]["a_m"]["gender"] == "men" and data["teams"]["a_w"]["gender"] == "women"
    assert option["clubRanks"]["club_a"], "club A has a place in the fairness rows"
    for row in option["clubRanks"]["club_a"]:
        assert row["by"] in {"team", "club"} and row["of"] >= len(row["found"])
        assert all(1 <= rank <= row["of"] for _, _, rank in row["found"])


def test_render_report_shows_the_playoff_weeks():
    import tempfile
    from pathlib import Path

    from terminliste.model.schema import PlayoffRound

    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comp_m.playoff_rounds = [
        PlayoffRound(id="po", label="Relegation play-off", dates=[date(2026, 12, 17), date(2026, 12, 21)])
    ]
    candidate = _candidate(
        world, season, [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")], []
    )

    with tempfile.TemporaryDirectory() as tmp:
        rendered = render_report(
            world, season, SolverResult(candidates=[candidate], solver="test"), Path(tmp) / "out.html"
        ).read_text(encoding="utf-8")

    assert "Play-off weeks" in rendered
    assert "Relegation play-off" in rendered
    assert "Thu 17 Dec 2026 (week 51)" in rendered
    assert "Mon 21 Dec 2026 (week 52)" in rendered


def test_rendered_score_explanation_states_the_scores_own_numbers():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [PreferredWeekday(competitions=[comp_m, comp_w])])
    score = candidate.score

    rendered = _render(world, season, [candidate])

    assert "How is this scored?" in rendered
    assert f"{score.soft_total:.1f} raw soft points over 1 match, {score.soft_per_match:.2f} per match" in rendered
    assert f"the score is {score.points:.1f}, shown as {score.points:.0f}" in rendered


def _sunday_and_saturday_candidate(world, season, comp_m, comp_w):
    """One Sunday match (on the preferred day) and one Saturday match (off it)."""
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 6), "vb"),
    ]
    constraints = [
        PreferredWeekday(competitions=[comp_m, comp_w]),
        ConsecutiveHomeDays(competitions=[comp_m, comp_w]),
        RestComfort(competitions=[comp_m, comp_w]),
        OneMatchPerTeamPerDay(),
    ]
    return _candidate(world, season, matches, constraints)


def test_rendered_rule_rows_show_achieved_of_possible_under_the_name():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    rendered = _render(world, season, [_sunday_and_saturday_candidate(world, season, comp_m, comp_w)])

    # Sunday's match is the one on the preferred day: 1 of the 2 league matches.
    assert "1 of 2 matches (50%)" in rendered
    assert 'class="coverage"' in rendered


def test_all_soft_rules_are_grouped_as_helping_hurting_and_neutral_with_a_net_line():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    candidate = _sunday_and_saturday_candidate(world, season, comp_m, comp_w)
    score = candidate.score
    soft = score.soft_results()
    helped = sum(r.total for r in soft if r.total > 0)
    hurt = sum(r.total for r in soft if r.total < 0)

    rendered = _render(world, season, [candidate])

    assert f"Show all {len(soft)} soft rules: helping, hurting, neutral" in rendered
    assert f'Helped by <b class="gain-text">+{helped:.1f}</b>, hurt by <b class="cost-text">+{hurt:.1f}</b>' in rendered
    assert f"net <b>+{score.soft_total:.1f}</b>." in rendered
    neutral = [r for r in soft if r.total == 0]
    assert "Helping (1)" in rendered and "Hurting (0)" in rendered
    assert f"Neutral, zero or did not apply ({len(neutral)})" in rendered


def test_satisfied_hard_rules_collapse_into_one_expandable_line():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    rendered = _render(world, season, [_sunday_and_saturday_candidate(world, season, comp_m, comp_w)])

    # Only one hard rule is in play, and it is satisfied.
    assert "1 hard rule satisfied ✓" in rendered


def test_score_explanation_gives_worked_examples_from_the_real_curve():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    rendered = _render(world, season, [_sunday_and_saturday_candidate(world, season, comp_m, comp_w)])

    assert "Per match, 0 gives 60; about 5.5 gives 89" in rendered
    assert "a red − takes them away" in rendered


def test_the_page_receives_the_season_movement_that_club_shares_are_built_on():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    candidate = _sunday_and_saturday_candidate(world, season, comp_m, comp_w)
    data = _report_data(_render(world, season, [candidate]))

    soft = candidate.score.soft_results()
    assert data["options"][0]["gross"] == round(sum(abs(r.total) for r in soft), 2)


# -- comparing the options (#35) ------------------------------------------------


def _options_for(world, candidates):
    """The option views the page builds, with the score text and tag each one shows."""
    dual = {club.id for club in world.dual_clubs()}
    rule_index = {rule_id: i for i, rule_id in enumerate(_rule_ids(candidates))}
    teams = payload.team_table(world)
    built = [
        _build_option(world, candidate, dual, [], _competition_colors(world), teams, rule_index)
        for candidate in candidates
    ]
    for option, (text, tag) in zip(built, _score_marks(built), strict=True):
        option["points_text"] = text
        option["tag"] = tag
    return built


def _comparison(world, candidates):
    return _comparison_view(world, candidates, _options_for(world, candidates), _rule_ids(candidates))


def _section(comparison, title):
    return next(section for section in comparison["sections"] if section["title"] == title)["rows"]


def _row(comparison, label, section_title="Overall"):
    rows = next(section for section in comparison["sections"] if section["title"] == section_title)["rows"]
    return next(row for row in rows if row["label"] == label)


def test_score_text_gets_a_decimal_when_whole_scores_collide_and_a_tie_is_tagged_as_one():
    assert _score_marks([{"points": 74.04}, {"points": 73.9}, {"points": 74.0}]) == [
        ("74.0", "tied top score"),
        ("73.9", None),
        ("74.0", "tied top score"),
    ]
    assert _score_marks([{"points": 61.6}, {"points": 73.4}]) == [("62", None), ("73", "top score")]
    assert _score_marks([{"points": 74.4}]) == [("74", None)]


def test_a_single_option_has_nothing_to_compare():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va")]
    only = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])])
    assert _comparison(world, [only]) is None
    assert 'class="compare"' not in _render(world, season, [only])
    assert 'class="tab-tag"' not in _render(world, season, [only])


def test_comparison_rows_show_each_candidates_own_contribution_to_the_score():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),  # Saturday, off the preferred Sunday
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 14), "vb", round_index=1),
    ]
    both = _candidate(world, season, matches, [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps)])
    home_only = _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=comps)])
    comparison = _comparison(world, [both, home_only])

    weekday = _row(comparison, rule_name("preferred_weekday"), "Soft points by rule, largest movement first")
    assert weekday["cells"][0]["text"] == _signed(both.score.result("preferred_weekday").total)
    # The second option never scored this rule: nothing to compare, so nothing is marked.
    assert weekday["cells"][1]["text"] == "—"
    assert weekday["best"] == [False, False]

    home = _row(comparison, rule_name("consecutive_home_days"), "Soft points by rule, largest movement first")
    assert [cell["text"] for cell in home["cells"]] == [
        _signed(both.score.result("consecutive_home_days").total),
        _signed(home_only.score.result("consecutive_home_days").total),
    ]
    soft_labels = {row["label"] for section in comparison["sections"] if section["title"].startswith("Soft") for row in section["rows"]}
    assert soft_labels == {rule_name("preferred_weekday"), rule_name("consecutive_home_days")}


def test_the_top_score_is_marked_and_the_option_with_hard_violations_is_weakest_on_them():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    clean = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
    ]
    # opp_m also plays club A's men on the same day: a hard break.
    clashing = clean + [f.match("elite", "opp_m", "a_m", date(2026, 6, 14), "vc", round_index=2)]
    constraints = [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps), OneMatchPerTeamPerDay()]
    good = _candidate(world, season, clean, constraints)
    bad = _candidate(world, season, clashing, constraints)
    assert good.score.hard_violations == 0 and bad.score.hard_violations > 0
    assert good.score.points > bad.score.points

    comparison = _comparison(world, [good, bad])
    assert [column["tag"] for column in comparison["columns"]] == ["top score", None]
    score = _row(comparison, "Score out of 100")
    assert [cell["best"] for cell in score["cells"]] == [True, False]
    hard = _row(comparison, "Hard-rule violations", "Hard rules")
    assert [cell["text"] for cell in hard["cells"]] == ["0", str(bad.score.hard_violations)]
    assert [cell["best"] for cell in hard["cells"]] == [True, False]
    assert comparison["columns"][0]["summary"].startswith("Leads on Hard-rule violations")
    assert "trails on Hard-rule violations (" in comparison["columns"][1]["summary"]


def test_three_options_get_three_columns_and_a_tie_for_the_top_is_tagged_on_each():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va")]
    candidates = [
        _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=comps)], label=f"Option {n}")
        for n in (1, 2, 3)
    ]
    comparison = _comparison(world, candidates)
    assert [column["label"] for column in comparison["columns"]] == ["Option 1", "Option 2", "Option 3"]
    assert [column["tag"] for column in comparison["columns"]] == ["tied top score"] * 3
    assert not any(cell["best"] for cell in _row(comparison, "Score out of 100")["cells"])


def test_fixture_differences_count_moved_swapped_and_retimed_fixtures_against_the_first_option():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    base = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
        f.match("elite", "b_m", "opp_m", date(2026, 6, 14), "vb", round_index=1),
    ]
    other = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 13), "va"),  # a week later
        f.match("topp", "opp_w", "a_w", date(2026, 6, 7), "vc"),  # the same pair, sides swapped
        f.match("elite", "b_m", "opp_m", date(2026, 6, 14), "vb", round_index=1, kickoff_time="18:00"),
        f.match("elite", "a_m", "b_m", date(2026, 6, 20), "va", round_index=2),  # a pairing the baseline lacks
    ]
    first = _candidate(world, season, base, [])
    second = _candidate(world, season, other, [])
    diff = _comparison(world, [first, second])["diffs"][0]

    assert diff["total"] == 4
    assert diff["count"] == 4
    assert diff["breakdown"] == (
        "1 on another date (side or kickoff may differ too), 1 with home and away swapped, "
        "1 with only the kickoff time changed, 1 not in the baseline."
    )
    details = [item["detail"] for item in diff["fixtures"]]  # in date order
    assert "now at home" in details[0]
    assert details[1] == "moved from Sat 6 Jun"
    assert details[2] == "kickoff unset → 18:00"
    assert details[3] == "not in the baseline option"


def test_identical_options_report_no_fixture_differences():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va")]
    candidates = [_candidate(world, season, matches, []), _candidate(world, season, matches, [])]
    diff = _comparison(world, candidates)["diffs"][0]
    assert diff["count"] == 0
    assert diff["breakdown"] == "No fixture differs from the baseline."


def test_the_page_shows_the_comparison_above_the_tabs_for_several_options():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va")]
    candidates = [
        _candidate(world, season, matches, [ConsecutiveHomeDays(competitions=comps)], label="Option 1"),
        _candidate(world, season, matches, [], label="Option 2"),
    ]
    rendered = _render(world, season, candidates)
    assert rendered.index('class="compare"') < rendered.index('class="tabs"')
    assert rendered.count('data-option="') == 2
    assert "Compare the 2 options" in rendered


def test_option_summaries_name_only_gaps_big_enough_to_matter_and_say_how_big():
    # The largest gap is 118 points (option 1 over option 3, back-to-back home days),
    # so gaps of 11.8 points or more are named; option 1's 5-point comfortable-rest
    # gap over option 3 is not.
    soft = [
        ("Back-to-back home days", [130.0, 10.0, 12.0]),
        ("Comfortable rest", [-20.0, -100.0, -15.0]),
    ]
    assert _option_summaries(soft, [0, 0, 0]) == [
        "Leads on Back-to-back home days (+118 pts).",
        "Trails on Back-to-back home days (−120 pts), Comfortable rest (−85 pts).",
        "Trails on Back-to-back home days (−118 pts).",
    ]


def test_option_summaries_name_hard_violation_gaps_by_their_count_first():
    summaries = _option_summaries([("Comfortable rest", [5.0, 5.0, -5.0])], [0, 0, 2])
    assert summaries[0] == "No row sets it apart."
    assert summaries[2] == "Trails on Hard-rule violations (2 more), Comfortable rest (−10 pts)."


def test_events_are_singular_only_for_one():
    assert _events(1) == "1 event"
    assert _events(2) == "2 events"


def test_rules_with_no_effect_in_any_option_are_listed_once_not_as_rows():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    # One match: no back-to-back home days in either option, so the rule adds nothing.
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va")]
    candidates = [_candidate(world, season, matches, [ConsecutiveHomeDays(competitions=comps)])] * 2
    comparison = _comparison(world, candidates)
    assert comparison["quiet"] == [rule_name("consecutive_home_days")]
    soft_labels = [row["label"] for section in comparison["sections"] if section["title"].startswith("Soft") for row in section["rows"]]
    assert rule_name("consecutive_home_days") not in soft_labels


def test_headline_rows_carry_no_sub_line_and_the_rule_each_headline_names():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    comps = [comp_m, comp_w]
    matches = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 14), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 14), "va"),
    ]
    candidate = _candidate(world, season, matches, [PreferredWeekday(competitions=comps), ConsecutiveHomeDays(competitions=comps)])
    assert {h["rule"] for h in _headlines(world, candidate)} == {"preferred_weekday", "consecutive_home_days"}
    comparison = _comparison(world, [candidate, candidate])
    for headline in _section(comparison, "Headline numbers"):
        assert all(cell["sub"] == "" for cell in headline["cells"])
