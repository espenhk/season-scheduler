"""Resolving a team's UEFA qualifying cascade to blocked leg dates."""

from __future__ import annotations

from datetime import date

import pytest

import factories as f
from terminliste.rounds.european_schedule import (
    EuropeanCascadeError,
    resolve_all_legs,
    resolve_european_commitments,
    resolve_leg_date,
    resolve_main_tournament_commitments,
    resolve_main_tournament_dates,
    resolve_qualifying_commitments,
    resolve_team_cascade,
)


def _legs(*competitions):
    """Resolve every leg of the given competitions with no blackouts —
    the shape `resolve_team_cascade` needs, built the way
    `resolve_european_commitments` builds it internally."""
    resolved, _ = resolve_all_legs(list(competitions), blackouts=set())
    return resolved


# -- resolve_leg_date ---------------------------------------------------------


def test_forced_leg_resolves_to_its_own_date_even_if_blacked_out():
    leg = f.european_leg(forced_date=date(2026, 8, 11))
    resolved, blacked_out = resolve_leg_date(leg, blackouts={date(2026, 8, 11)})
    assert resolved == date(2026, 8, 11)
    assert blacked_out is True


def test_windowed_leg_resolves_to_its_earliest_non_blackout_day():
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5))
    resolved, blacked_out = resolve_leg_date(leg, blackouts={date(2026, 8, 4)})
    assert resolved == date(2026, 8, 5)
    assert blacked_out is False


def test_windowed_leg_falls_back_to_window_start_when_every_day_is_blacked_out():
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5))
    resolved, blacked_out = resolve_leg_date(leg, blackouts={date(2026, 8, 4), date(2026, 8, 5)})
    assert resolved == date(2026, 8, 4)
    assert blacked_out is True


# -- resolve_leg_date: preferred_weekday (issue #79) --------------------------


def test_preferred_weekday_is_picked_over_the_plain_earliest_day():
    # 2026-08-04 is a Tuesday, 2026-08-06 a Thursday — without a preference
    # the earliest day (Tuesday) would win.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 7))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), preferred_weekday="thursday")
    assert resolved == date(2026, 8, 6)
    assert blacked_out is False


def test_preferred_weekday_falls_back_to_earliest_day_when_absent_from_window():
    # Tuesday-Wednesday only — no Thursday in this window at all.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), preferred_weekday="thursday")
    assert resolved == date(2026, 8, 4)
    assert blacked_out is False


def test_preferred_weekday_skips_a_blacked_out_occurrence_for_a_later_one():
    # Two Thursdays in range: 2026-08-06 and 2026-08-13.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 13))
    resolved, blacked_out = resolve_leg_date(
        leg, blackouts={date(2026, 8, 6)}, preferred_weekday="thursday"
    )
    assert resolved == date(2026, 8, 13)
    assert blacked_out is False


def test_preferred_weekday_never_overrides_a_forced_date():
    # 2026-08-04 is a Tuesday, not a Thursday — a forced date is used
    # exactly regardless of any weekday preference.
    leg = f.european_leg(forced_date=date(2026, 8, 4))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), preferred_weekday="thursday")
    assert resolved == date(2026, 8, 4)
    assert blacked_out is False


# -- resolve_team_cascade: a single competition, no cascade ------------------


def test_single_round_entry_gives_both_leg_dates():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "playoff",
                entrants=["glimt"],
                first_leg=f.european_leg(forced_date=date(2026, 8, 18)),
                second_leg=f.european_leg(forced_date=date(2026, 8, 25)),
            )
        ],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    assert [c.date for c in commitments] == [date(2026, 8, 18), date(2026, 8, 25)]


def test_win_progression_follows_the_next_round_when_the_team_is_listed():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round("q3", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            f.european_round("playoff", entrants=["glimt"], forced_date=date(2026, 8, 18)),
        ],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    # Both legs of q3 (same forced_date for the test), then both legs of
    # the play-off.
    assert [c.date for c in commitments] == [
        date(2026, 8, 4),
        date(2026, 8, 4),
        date(2026, 8, 18),
        date(2026, 8, 18),
    ]


def test_win_progression_stops_when_the_team_is_not_listed_in_the_next_round():
    """Viking-shaped case: a team entering a later round directly must not
    be treated as having "progressed" from an earlier one it never played."""
    comp = f.competition(
        "cl",
        ["glimt", "viking"],
        format="european",
        european_rounds=[
            f.european_round("q3", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            f.european_round(
                "playoff", entrants=["glimt", "viking"], forced_date=date(2026, 8, 18)
            ),
        ],
    )
    viking_commitments = resolve_team_cascade("viking", comp, {"cl": comp}, _legs(comp))
    assert len(viking_commitments) == 2
    assert all(c.date == date(2026, 8, 18) for c in viking_commitments)


def test_win_progression_scans_past_a_round_the_team_is_not_entrant_of():
    """A competition's `european_rounds` can interleave two UEFA paths —
    e.g. a Champions-Path round sitting between two League-Path ones, as
    champions_league_2026.yml's own header describes. A team's next round
    is whichever round *it* next appears in, not necessarily the very next
    list entry, which might belong to the other path entirely."""
    comp = f.competition(
        "cl",
        ["glimt", "viking"],
        format="european",
        european_rounds=[
            f.european_round("q3_league_path", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            # Champions-Path round, sitting between two League-Path ones —
            # glimt (League Path) is not an entrant of this one.
            f.european_round("q3_champions_path", entrants=["viking"], forced_date=date(2026, 8, 6)),
            f.european_round("playoff", entrants=["glimt", "viking"], forced_date=date(2026, 8, 18)),
        ],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    assert len(commitments) == 4  # q3_league_path's two legs, then playoff's two
    assert commitments[-1].date == date(2026, 8, 18), "glimt's play-off commitment must not be dropped"


def test_chain_ends_when_no_further_round_or_drop_to_applies():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[f.european_round("playoff", entrants=["glimt"], forced_date=date(2026, 8, 18))],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    assert len(commitments) == 2


def test_entrant_missing_from_every_round_raises():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[f.european_round("q3", entrants=["someone_else"], forced_date=date(2026, 8, 4))],
    )
    with pytest.raises(EuropeanCascadeError, match="not an entrant"):
        resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))


# -- resolve_team_cascade: cascading into another competition ----------------


def test_drop_to_cascades_into_the_named_round_of_another_competition():
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["tromso"],
                forced_date=date(2026, 8, 6),
                drop_to_competition="uecl",
                drop_to_round="playoff",
            )
        ],
    )
    uecl = f.competition(
        "uecl",
        ["brann", "tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "playoff",
                entrants=["brann", "tromso"],
                forced_date=date(2026, 8, 20),
            )
        ],
    )
    commitments = resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))
    assert [c.date for c in commitments] == [
        date(2026, 8, 6),
        date(2026, 8, 6),
        date(2026, 8, 20),
        date(2026, 8, 20),
    ]
    assert commitments[0].label == "el: q3 (first leg)"
    assert commitments[2].label == "uecl: playoff (first leg)"
    # q3 has no win-progression round to fork against — dropping is the
    # only live continuation, so it stays certain the whole way down.
    assert all(c.certain for c in commitments)
    assert all(c.competition_id == "el" for c in commitments[:2])
    assert all(c.competition_id == "uecl" for c in commitments[2:])


def test_a_fork_keeps_uncertainty_for_every_round_further_down_that_branch():
    """Once a branch is only reachable via a fork, everything further down
    that same branch stays uncertain too — not knowing you're even on the
    branch makes anything beyond the fork just as conditional, even a round
    reached by a plain, non-forking win-progression from there."""
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["tromso"],
                forced_date=date(2026, 8, 6),
                drop_to_competition="uecl",
                drop_to_round="playoff",
            ),
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 22)),
        ],
    )
    uecl = f.competition(
        "uecl",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 18)),
            # Reached from uecl's playoff by plain win-progression alone —
            # no fork here — but the branch it's on is already uncertain.
            f.european_round("final", entrants=["tromso"], forced_date=date(2026, 8, 25)),
        ],
    )
    commitments = resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))
    by_label = {c.label: c for c in commitments}
    assert by_label["el: q3 (first leg)"].certain is True
    assert by_label["el: playoff (first leg)"].certain is False
    assert by_label["uecl: playoff (first leg)"].certain is False
    assert by_label["uecl: final (first leg)"].certain is False


def test_win_and_drop_branches_at_the_same_depth_both_produce_commitments():
    """A team that could either advance within its own competition or drop
    into another one's equivalent round has both branches open until a
    result narrows it down — the resolver must block dates from both, not
    pick one arbitrarily.

    Since issue #93, only the pre-fork commitment (q3, played whatever
    happens next) is `certain`; both post-fork branches are mutually
    exclusive — a team either wins q3 and advances, or loses it and drops,
    never both — so neither counts as a guaranteed fixture on its own."""
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["tromso"],
                forced_date=date(2026, 8, 6),
                drop_to_competition="uecl",
                drop_to_round="playoff",
            ),
            # Win branch: stays in the Europa League.
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 22)),
        ],
    )
    uecl = f.competition(
        "uecl",
        ["tromso"],
        format="european",
        european_rounds=[
            # Drop branch: a different date than the win branch above.
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 18))
        ],
    )
    commitments = resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))
    # depth 0 (q3): 2 commitments. depth 1: both branches' play-off legs, 4
    # commitments (2 from el's own playoff, 2 from uecl's).
    assert len(commitments) == 6
    depth0, depth1 = commitments[:2], commitments[2:]

    assert all(c.certain for c in depth0), "the pre-fork q3 commitment is certain"
    assert not any(c.certain for c in depth1), "neither post-fork branch is certain on its own"

    depth1_dates = {c.date for c in depth1}
    assert depth1_dates == {date(2026, 8, 22), date(2026, 8, 18)}
    depth1_labels = {c.label for c in depth1}
    assert depth1_labels == {
        "el: playoff (first leg)",
        "el: playoff (second leg)",
        "uecl: playoff (first leg)",
        "uecl: playoff (second leg)",
    }


def test_min_rest_days_comes_from_each_commitments_own_competition():
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        min_rest_days=3,
        european_rounds=[
            f.european_round(
                "q3", entrants=["tromso"], forced_date=date(2026, 8, 6),
                drop_to_competition="uecl", drop_to_round="playoff",
            ),
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 22)),
        ],
    )
    uecl = f.competition(
        "uecl",
        ["tromso"],
        format="european",
        min_rest_days=6,
        european_rounds=[f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 18))],
    )
    commitments = resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))
    by_label = {c.label: c for c in commitments}
    assert by_label["el: playoff (first leg)"].min_rest_days == 3
    assert by_label["uecl: playoff (first leg)"].min_rest_days == 6


def test_dangling_drop_to_competition_raises():
    comp = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3", entrants=["tromso"], forced_date=date(2026, 8, 6),
                drop_to_competition="ghost", drop_to_round="whatever",
            )
        ],
    )
    with pytest.raises(EuropeanCascadeError, match="unknown competition"):
        resolve_team_cascade("tromso", comp, {"el": comp}, _legs(comp))


def test_dangling_drop_to_round_raises():
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3", entrants=["tromso"], forced_date=date(2026, 8, 6),
                drop_to_competition="uecl", drop_to_round="ghost_round",
            )
        ],
    )
    uecl = f.competition(
        "uecl", ["brann"], format="european",
        european_rounds=[f.european_round("q2", entrants=["brann"], forced_date=date(2026, 7, 21))],
    )
    with pytest.raises(EuropeanCascadeError, match="does not exist there"):
        resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))


# -- resolve_european_commitments: season-level orchestration ----------------


def test_resolve_european_commitments_resolves_every_direct_entrant():
    cl = f.competition(
        "cl",
        ["glimt", "viking"],
        format="european",
        european_rounds=[
            f.european_round("q3", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            f.european_round("playoff", entrants=["glimt", "viking"], forced_date=date(2026, 8, 18)),
        ],
    )
    season = f.season(european_competitions=["cl"])
    result, warnings = resolve_european_commitments([cl], season)
    assert set(result) == {"glimt", "viking"}
    assert len(result["glimt"]) == 4
    assert len(result["viking"]) == 2
    assert warnings == []


def test_resolve_european_commitments_does_not_double_resolve_a_cascade_landing_spot():
    """`tromso` is a real entrant of `el` and only a cascade landing spot in
    `uecl` — iterating `uecl.teams` must not treat `uecl`'s playoff round as
    a second, independent home for `tromso`, which would silently overwrite
    the correct (longer) result from `el` with the incomplete one from the
    drop target."""
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3", entrants=["tromso"], forced_date=date(2026, 8, 6),
                drop_to_competition="uecl", drop_to_round="playoff",
            )
        ],
    )
    uecl = f.competition(
        "uecl",
        ["brann", "tromso"],
        format="european",
        european_rounds=[
            f.european_round("q2", entrants=["brann"], forced_date=date(2026, 7, 21)),
            f.european_round("playoff", entrants=["brann", "tromso"], forced_date=date(2026, 8, 20)),
        ],
    )
    season = f.season(european_competitions=["el", "uecl"])
    result, _ = resolve_european_commitments([el, uecl], season)
    assert len(result["tromso"]) == 4, "tromso's el-then-uecl cascade must survive intact"
    assert len(result["brann"]) == 4


def test_a_direct_entrant_is_not_dropped_when_its_own_round_is_also_a_different_teams_drop_target():
    """Regression: the skip in `resolve_european_commitments` used to be
    keyed on (competition, round) alone. When some other team's cascade
    happens to land on exactly the round a different team enters directly
    — a real UEFA shape, not a contrived one — that keying incorrectly
    skipped the direct entrant too, silently dropping it out of the result
    with no error and no commitments to block its league matches with."""
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "q2", entrants=["tromso"], forced_date=date(2026, 7, 23),
                # Drops into brann's own direct entry round in uecl below.
                drop_to_competition="uecl", drop_to_round="q2",
            )
        ],
    )
    uecl = f.competition(
        "uecl",
        ["brann"],
        format="european",
        european_rounds=[f.european_round("q2", entrants=["brann"], forced_date=date(2026, 7, 21))],
    )
    season = f.season(european_competitions=["el", "uecl"])
    result, _ = resolve_european_commitments([el, uecl], season)
    assert "brann" in result, "brann's direct uecl entry must survive being a tromso drop target too"
    assert len(result["brann"]) == 2
    assert len(result["tromso"]) == 4


def test_resolve_european_commitments_avoids_a_blacked_out_leg_and_warns():
    from terminliste.model.schema import DatedNote

    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["glimt"],
                first_leg=f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5)),
                second_leg=f.european_leg(forced_date=date(2026, 8, 11)),
            )
        ],
    )
    season = f.season(
        european_competitions=["cl"],
        global_blackouts=[
            DatedNote(date=date(2026, 8, 4)),
            DatedNote(date=date(2026, 8, 11)),
        ],
    )
    result, warnings = resolve_european_commitments([comp], season)
    dates = [c.date for c in result["glimt"]]
    assert dates == [date(2026, 8, 5), date(2026, 8, 11)], (
        "the window resolves around the blackout; the forced date does not move"
    )
    assert len(warnings) == 1
    assert "second leg" in warnings[0]


def test_resolve_european_commitments_avoids_a_global_blackout_range():
    from terminliste.model.schema import GlobalBlackoutRange

    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["glimt"],
                first_leg=f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5)),
                second_leg=f.european_leg(forced_date=date(2026, 8, 11)),
            )
        ],
    )
    season = f.season(
        european_competitions=["cl"],
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 8, 1), end=date(2026, 8, 4), reason="international break"
            )
        ],
    )
    result, warnings = resolve_european_commitments([comp], season)
    dates = [c.date for c in result["glimt"]]
    assert dates == [date(2026, 8, 5), date(2026, 8, 11)], (
        "the window resolves around the excluded range; the forced date does not move"
    )
    assert warnings == []


def test_resolve_european_commitments_ignores_venue_blackout_ranges():
    """European qualifiers don't book venues — a `venue_blackout_ranges`
    entry must not influence leg resolution, the same as `venue_blackouts`
    already doesn't (see `resolve_european_commitments`'s docstring)."""
    from terminliste.model.schema import VenueBlackoutRange

    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3",
                entrants=["glimt"],
                first_leg=f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5)),
                second_leg=f.european_leg(forced_date=date(2026, 8, 11)),
            )
        ],
    )
    season = f.season(
        european_competitions=["cl"],
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="v1", start=date(2026, 8, 4), end=date(2026, 8, 4), reason="ground works"
            )
        ],
    )
    result, warnings = resolve_european_commitments([comp], season)
    dates = [c.date for c in result["glimt"]]
    assert dates == [date(2026, 8, 4), date(2026, 8, 11)]
    assert warnings == []


def test_cli_report_data_and_solver_commitments_agree_on_excluded_range_dates():
    """Regression: `cli.py::_european_report_data` (the report's display
    path) used to build its own blackout set from `season.global_blackouts`
    alone, so a windowed leg overlapping a `global_blackout_ranges` entry
    resolved to a different date there than in
    `resolve_european_commitments` (what the solver treats as the real
    commitment) — the report would show a leg on a date the scheduler
    believes it isn't played on."""
    import cli
    from terminliste.model.schema import GlobalBlackoutRange

    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "q2",
                entrants=["glimt"],
                first_leg=f.european_leg(window_start=date(2026, 7, 21), window_end=date(2026, 7, 22)),
                second_leg=f.european_leg(forced_date=date(2026, 7, 28)),
            )
        ],
    )
    season = f.season(
        european_competitions=["cl"],
        global_blackout_ranges=[
            GlobalBlackoutRange(start=date(2026, 7, 21), end=date(2026, 7, 21), reason="break")
        ],
    )
    world = f.world([], [], [comp])

    commitments, _ = resolve_european_commitments([comp], season)
    commitment_dates = tuple(c.date for c in commitments["glimt"])

    _, resolved_legs = cli._european_report_data(world, season)
    report_dates = resolved_legs[("cl", "q2")]

    assert commitment_dates == report_dates == (date(2026, 7, 22), date(2026, 7, 28))


# -- main tournaments (issue #79) ----------------------------------------------


def test_resolve_main_tournament_dates_labels_matchdays_and_knockout_legs():
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
            f.main_tournament_round(
                "final", forced_date=date(2027, 6, 5), venue_name="Wembley Stadium"
            ),
        ],
    )
    dated, warnings = resolve_main_tournament_dates(main, blackouts=set())
    assert dated == [
        ("cl_main: md1", date(2026, 9, 10)),
        ("cl_main: r16 (first leg)", date(2027, 3, 10)),
        ("cl_main: r16 (second leg)", date(2027, 3, 17)),
        ("cl_main: final", date(2027, 6, 5)),
    ]
    assert warnings == []


def test_resolve_main_tournament_dates_applies_preferred_weekday():
    main = f.competition(
        "cl_main",
        [],
        format="european",
        preferred_weekday="thursday",
        is_main_tournament=True,
        league_phase_matchdays=[
            f.european_matchday("md1", window_start=date(2026, 8, 4), window_end=date(2026, 8, 7))
        ],
    )
    dated, _ = resolve_main_tournament_dates(main, blackouts=set())
    assert dated == [("cl_main: md1", date(2026, 8, 6))]  # the Thursday in that window


def _cl_qualifying(entrants_by_round: dict[str, list[str]]):
    return f.competition(
        "cl_q",
        sorted({team for teams in entrants_by_round.values() for team in teams}),
        format="european",
        european_rounds=[
            f.european_round(round_id, entrants=teams, forced_date=date(2026, 8, 4))
            for round_id, teams in entrants_by_round.items()
        ],
    )


def test_resolve_main_tournament_commitments_blocks_every_reachable_team():
    qualifying = _cl_qualifying({"q1": ["glimt"], "playoff": ["glimt", "viking"]})
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["cl_q"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
        knockout_rounds=[
            f.main_tournament_round("final", forced_date=date(2027, 6, 5), venue_name="Wembley")
        ],
    )
    qualifying_commitments, _ = resolve_qualifying_commitments([qualifying], f.season())
    result, warnings = resolve_main_tournament_commitments(
        [qualifying, main], f.season(), qualifying_commitments
    )
    assert set(result) == {"glimt", "viking"}
    for team in ("glimt", "viking"):
        assert {c.date for c in result[team]} == {date(2026, 9, 10), date(2027, 6, 5)}
        assert all(c.min_rest_days == main.min_rest_days for c in result[team])
        # Neither team's route to cl_main forks (glimt's q1 -> playoff is a
        # single continuation; viking enters playoff directly), so both
        # blocks stay certain (hard).
        assert all(c.certain for c in result[team])
    assert warnings == []


def test_resolve_main_tournament_commitments_skips_a_team_not_in_reachable_from():
    cl_qualifying = _cl_qualifying({"playoff": ["glimt"]})
    el_qualifying = f.competition(
        "el_q",
        ["other_team"],
        format="european",
        european_rounds=[
            f.european_round("playoff", entrants=["other_team"], forced_date=date(2026, 8, 20))
        ],
    )
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["cl_q"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    qualifying_commitments, _ = resolve_qualifying_commitments(
        [cl_qualifying, el_qualifying], f.season()
    )
    result, _ = resolve_main_tournament_commitments(
        [cl_qualifying, el_qualifying, main], f.season(), qualifying_commitments
    )
    assert set(result) == {"glimt"}


def test_resolve_main_tournament_commitments_softens_a_team_forked_between_two_main_tournaments():
    """Tromsø-shaped case (issue #93): a team one drop hop from a second
    competition's qualifying is reachable for *two* main tournaments — its
    own (by winning through) and the drop target's (by losing and winning
    from there) — but never both, since winning q3 and dropping from q3 are
    mutually exclusive. Neither block should be a hard exclusion on its
    own; both are still blocked, just as soft, conditional commitments."""
    el = f.competition(
        "el",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round("q2", entrants=["tromso"], forced_date=date(2026, 7, 23)),
            f.european_round(
                "q3",
                entrants=["tromso"],
                forced_date=date(2026, 8, 6),
                drop_to_competition="uecl",
                drop_to_round="playoff",
            ),
            # Win branch: stays in the Europa League.
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 20)),
        ],
    )
    uecl = f.competition(
        "uecl",
        ["tromso"],
        format="european",
        european_rounds=[
            # Drop branch: Tromsø's landing spot if eliminated from el's q3.
            f.european_round("playoff", entrants=["tromso"], forced_date=date(2026, 8, 20)),
        ],
    )
    el_main = f.competition(
        "el_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["el"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 10, 15))],
    )
    uecl_main = f.competition(
        "uecl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["uecl"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 10, 15))],
    )
    competitions = [el, uecl, el_main, uecl_main]
    qualifying_commitments, _ = resolve_qualifying_commitments(competitions, f.season())
    result, _ = resolve_main_tournament_commitments(competitions, f.season(), qualifying_commitments)

    assert set(result) == {"tromso"}
    assert len(result["tromso"]) == 2, "blocked against both main tournaments, not deduplicated"
    assert not any(c.certain for c in result["tromso"]), (
        "el's q3 forks into win-progression and the uecl drop, so neither main "
        "tournament is a guaranteed destination on its own"
    )


def test_resolve_european_commitments_merges_qualifying_cascade_and_main_tournament():
    qualifying = _cl_qualifying({"playoff": ["glimt"]})
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["cl_q"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    season = f.season(european_competitions=["cl_q", "cl_main"])
    commitments_by_team, warnings = resolve_european_commitments([qualifying, main], season)
    dates = sorted(c.date for c in commitments_by_team["glimt"])
    # The playoff round's two legs (same forced_date for both, per
    # `_cl_qualifying`'s single forced_date) plus the main tournament's one
    # league-phase matchday.
    assert dates == [date(2026, 8, 4), date(2026, 8, 4), date(2026, 9, 10)]
    assert warnings == []
