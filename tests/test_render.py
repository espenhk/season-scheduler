"""Report rendering: fairness aggregation, combined views, and the generated
HTML page's new controls (issues #21-25, #39)."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import date

import factories as f
import pytest
from terminliste.report.render import (
    _club_headlines,
    _cup_entries,
    _day_dots,
    _entity_counts,
    _european_entries,
    _fairness_rows,
    _json_attr,
    _match_entries,
    _month_calendar_view,
    _search_stats_view,
    _tint,
    render_report,
)
from terminliste.scoring.base import ConstraintResult, EvalContext, Event, evaluate
from terminliste.scoring.soft import ConsecutiveHomeDays
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


# -- HTML-attribute-safe JSON ----------------------------------------------


def test_json_attr_escapes_quotes_so_the_json_survives_an_html_attribute():
    """`season.html.j2` embeds this as `data-club="{{ option.club_headlines_json }}"`
    — a literal `"` from plain `json.dumps` would terminate the attribute
    early and corrupt the markup. `_json_attr` must escape it, and a browser
    unescaping the attribute back (`html.unescape`, standing in for the
    DOM's `.dataset` decoding) must recover the original JSON exactly."""
    payload = {"club <b>&</b> co": [{"value": "1", "label": 'on "Sunday"'}]}

    attr_value = _json_attr(payload)

    assert '"' not in attr_value
    assert "<" not in attr_value
    assert json.loads(html.unescape(attr_value)) == payload


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
    assert entry["competition_short"] == comp_m.short_name


def test_combined_calendar_interleaves_both_competitions_in_the_same_week():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    same_week = [
        f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va"),
        f.match("topp", "a_w", "opp_w", date(2026, 6, 7), "va"),
    ]
    candidate = _candidate(world, season, same_week, [])

    result = SolverResult(candidates=[candidate], solver="test")
    import tempfile
    from pathlib import Path

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


def test_cup_entries_flatten_one_row_per_entered_team_per_round():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    club_colors = {c.id: c.color for c in world.clubs.values()}
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

    entries = _cup_entries(world, [schedule], club_colors, competition_colors)

    assert len(entries) == 2
    entry = next(e for e in entries if e["team_full"] == world.team_label("a_m"))
    assert entry["is_cup"] is True
    assert entry["competition_id"] == "cup1"
    assert entry["competition_name"] == "NM Cup"
    assert entry["competition_short"] == "NM"
    assert entry["round_name"] == "Round 1"
    assert entry["club_id"] == "club_a"
    assert entry["venue_type"] == "away"
    assert entry["date"] == date(2026, 3, 1)
    assert entry["comp_fg"] == "#111111"


def test_combined_views_include_cup_rounds_alongside_league_matches():
    """A cup round used to only appear under "By competition" — issue: it must
    also show up in the combined calendar and combined list views, the same
    place a reader checks for "what's happening this week" across everything."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    schedule = f.cup_schedule(
        "cup1",
        [f.cup_placement("r1", {"a_m": date(2026, 6, 6)}, round_name="Round 1", venue_type="away")],
        competition_name="NM Cup",
    )
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html", cup_schedules=[schedule])
        html = out.read_text(encoding="utf-8")

    calendar_section = html.split('class="cal-wrap view-pane" data-view="combined-calendar"')[1].split(
        'class="cal-wrap view-pane" data-view="combined-list"'
    )[0]
    list_section = html.split('class="cal-wrap view-pane" data-view="combined-list"')[1]

    assert "NM Cup" in calendar_section
    assert "v TBD" in calendar_section
    assert "NM Cup" in list_section
    assert "TBD (Round 1)" in list_section


def test_european_entries_flatten_one_row_per_entered_team_per_leg():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    club_colors = {c.id: c.color for c in world.clubs.values()}
    competition_colors = {"cl": {"fg": "#111111", "bg": "#eeeeee"}}
    round_ = f.european_round(
        "q3",
        first_leg=f.european_leg(forced_date=date(2026, 8, 4)),
        second_leg=f.european_leg(forced_date=date(2026, 8, 11)),
        ties=[f.european_tie("a_m", opponent="Union Saint-Gilloise", home_leg="second")],
    )
    comp = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "q3"): (date(2026, 8, 4), date(2026, 8, 11))}

    entries = _european_entries(world, [comp], resolved_legs, club_colors, competition_colors)

    assert len(entries) == 2
    first_leg_entry = next(e for e in entries if e["date"] == date(2026, 8, 4))
    assert first_leg_entry["is_european"] is True
    assert first_leg_entry["competition_id"] == "cl"
    assert first_leg_entry["opponent"] == "Union Saint-Gilloise"
    assert first_leg_entry["club_id"] == "club_a"
    # home_leg="second" -> the first leg is away for this team.
    assert first_leg_entry["venue_type"] == "away"
    assert first_leg_entry["comp_fg"] == "#111111"
    assert first_leg_entry["competition_short"] == comp.short_name

    second_leg_entry = next(e for e in entries if e["date"] == date(2026, 8, 11))
    assert second_leg_entry["venue_type"] == "home"


def test_european_entries_use_unknown_venue_type_when_home_leg_is_not_set():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    club_colors = {c.id: c.color for c in world.clubs.values()}
    round_ = f.european_round(
        "playoff",
        forced_date=date(2026, 8, 18),
        ties=[f.european_tie("a_m")],  # opponent/home_leg left at their defaults
    )
    comp = f.competition("cl", ["a_m"], format="european", european_rounds=[round_])
    resolved_legs = {("cl", "playoff"): (date(2026, 8, 18), date(2026, 8, 18))}

    entries = _european_entries(world, [comp], resolved_legs, club_colors, {})
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
    club_colors = {c.id: c.color for c in world.clubs.values()}
    qualifying = f.competition(
        "cl",
        ["a_m"],
        format="european",
        european_rounds=[f.european_round("playoff", entrants=["a_m"], forced_date=date(2026, 8, 18))],
    )
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["cl"],
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

    rounds, resolved_legs = build_main_tournament_rounds_for_display(main, [qualifying, main], set())
    display_main = main.model_copy(update={"european_rounds": rounds})

    entries = _european_entries(world, [display_main], resolved_legs, club_colors, {})
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

    views = _european_views(world, [display_main], resolved_legs, club_colors)
    assert len(views) == 1
    assert views[0]["is_main_tournament"] is True
    assert views[0]["match_count"] == 3
    matchday_round = next(r for r in views[0]["rounds"] if r["name"] == "md1")
    assert len(matchday_round["fixtures"]) == 1
    r16_round = next(r for r in views[0]["rounds"] if r["name"] == "r16")
    assert len(r16_round["fixtures"]) == 2
    final_round = next(r for r in views[0]["rounds"] if r["name"] == "final")
    assert len(final_round["fixtures"]) == 1
    assert "Wembley Stadium" in final_round["note"]


def test_combined_views_include_european_rounds_alongside_league_matches():
    """A European round used to be invisible in the report — it must show up
    both under "By competition" and in the combined calendar/list views,
    with the real opponent shown where the data has one."""
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
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(
            world,
            season,
            result,
            Path(tmp) / "out.html",
            european_competitions=[comp],
            resolved_legs=resolved_legs,
        )
        html = out.read_text(encoding="utf-8")

    calendar_section = html.split('class="cal-wrap view-pane" data-view="combined-calendar"')[1].split(
        'class="cal-wrap view-pane" data-view="combined-list"'
    )[0]
    list_section = html.split('class="cal-wrap view-pane" data-view="combined-list"')[1]

    assert "Union Saint-Gilloise" in calendar_section
    assert "Union Saint-Gilloise" in list_section


# -- month calendar (issue #77) -------------------------------------------


def test_tint_lightens_a_hex_color_toward_white():
    assert _tint("#000000", factor=0.5) == "#808080"
    assert _tint("#000000", factor=1.0) == "#ffffff"


def test_day_dots_deduplicates_multiple_entries_from_the_same_competition():
    """Two Eliteserien matches on the same day still show one dot — issue
    #77 asks for "only one dot per league per day" — but the dot still
    collects every club that played, for the club filter (issue #77
    review)."""
    entries = [
        {"competition_id": "elite", "competition_name": "Eliteserien", "comp_fg": "#ca6f1e",
         "home_club": "club_a", "away_club": "club_b"},
        {"competition_id": "elite", "competition_name": "Eliteserien", "comp_fg": "#ca6f1e",
         "home_club": "club_c", "away_club": "club_d"},
        {"competition_id": "topp", "competition_name": "Toppserien", "comp_fg": "#8e44ad",
         "home_club": "club_a", "away_club": "club_e"},
    ]
    assert _day_dots(entries) == [
        {"name": "Eliteserien", "color": "#ca6f1e", "club_ids": ["club_a", "club_b", "club_c", "club_d"]},
        {"name": "Toppserien", "color": "#8e44ad", "club_ids": ["club_a", "club_e"]},
    ]


def test_day_dots_reads_club_id_off_cup_and_european_entries():
    """The other two combined-view entry shapes (cup, European) carry
    `club_id` rather than `home_club`/`away_club` — `_day_dots` must read
    whichever one an entry actually has."""
    entries = [
        {"competition_id": "cup", "competition_name": "NM Cup", "comp_fg": "#c0392b", "club_id": "club_a"},
        {"competition_id": "cl", "competition_name": "Champions League", "comp_fg": "#0b2c40", "club_id": "club_b"},
    ]
    dots = {d["name"]: d["club_ids"] for d in _day_dots(entries)}
    assert dots == {"NM Cup": ["club_a"], "Champions League": ["club_b"]}


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
    matched = [day for week in month["weeks"] for day in week if day["date_iso"] == "2026-03-01"]
    assert len(matched) == 1
    assert matched[0]["in_month"] is True
    assert matched[0]["dots"] == [{"name": "Eliteserien", "color": "#ca6f1e", "club_ids": ["club_a", "club_b"]}]

    assert len(day_details) == 1
    assert day_details[0]["date_iso"] == "2026-03-01"
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
    # 1 April also shows up as an out-of-month padding cell at the tail of
    # March's grid, carrying the same dot — but only one day_details entry.
    march_padding = [
        day for week in months[0]["weeks"] for day in week if day["date_iso"] == "2026-04-01"
    ]
    assert march_padding and march_padding[0]["in_month"] is False
    assert march_padding[0]["dots"] == [
        {"name": "Toppserien", "color": "#8e44ad", "club_ids": ["club_c", "club_d"]}
    ]


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


def test_render_report_includes_month_calendar_with_a_dot_and_tappable_day():
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert "Month calendar" in rendered
    assert 'data-view="month-calendar"' in rendered
    assert 'data-date="2026-06-06"' in rendered
    assert 'class="cal-dot"' in rendered


def test_render_report_month_calendar_carries_club_ids_and_a_color_legend():
    """Issue #77 review: the club filter (`applyClubFilter` in
    season.html.j2) sweeps every `[data-clubs]` element in the panel, so the
    calendar's dots and day-detail rows need that attribute too, or
    filtering by club would leave the calendar showing everyone's games."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    assert 'class="cal-dot" data-clubs="club_a opp1"' in rendered
    assert 'class="day-row"' in rendered
    assert 'data-clubs="club_a opp1"' in rendered
    # The colour legend names the competition next to a swatch of its colour
    # (factories.competition() sets `name=id`, so "elite" is the real name here).
    calendar_pane = rendered.split('class="cal-wrap view-pane" data-view="month-calendar"')[1].split("<h4>")[0]
    assert "elite" in calendar_pane
    assert '<span class="cal-dot" style="background:' in calendar_pane


def test_render_report_month_calendar_day_detail_uses_the_competition_short_name():
    """The month calendar's day-detail rows are the cramped spot this exists
    for: the comp-tag shows the short name, with the full name still
    reachable via a title tooltip."""
    world, season, comp_m, comp_w = _two_dual_clubs_world()
    matches = [f.match("elite", "a_m", "opp_m", date(2026, 6, 6), "va")]
    candidate = _candidate(world, season, matches, [])
    result = SolverResult(candidates=[candidate], solver="test")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = render_report(world, season, result, Path(tmp) / "out.html")
        rendered = out.read_text(encoding="utf-8")

    day_detail = rendered.split('data-date="2026-06-06" hidden>')[1].split("</div>\n    </div>")[0]
    assert f'title="{comp_m.name}">{comp_m.short_name}<' in day_detail


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
    assert "min rest days" in rendered


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
