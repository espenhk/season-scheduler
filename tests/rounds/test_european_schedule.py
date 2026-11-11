"""Resolving a team's UEFA path to an outcome tree and its commitments."""

from __future__ import annotations

from datetime import date

import factories as f
import pytest

from terminliste.rounds.european_schedule import (
    EuropeanCascadeError,
    resolve_all_legs,
    resolve_european_commitments,
    resolve_european_trees,
    resolve_leg_date,
    resolve_main_stages,
    resolve_team_cascade,
)


def _by_team(commitments):
    """A `CommitmentSet` regrouped as {team: [commitment, ...]}."""
    grouped: dict[str, list] = {}
    for commitment in commitments:
        grouped.setdefault(commitment.team_id, []).append(commitment)
    return grouped


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


# -- resolve_leg_date: matchday_weekdays (#79, #112) -------------------------


def test_matchday_weekday_is_picked_over_the_plain_earliest_day():
    # 2026-08-04 is a Tuesday, 2026-08-06 a Thursday — without a matchday
    # the earliest day (Tuesday) would win.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 7))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), matchday_weekdays=["thursday"])
    assert resolved == date(2026, 8, 6)
    assert blacked_out is False


def test_matchday_weekdays_are_tried_in_preference_order():
    # Tue 4 - Thu 6 Aug: Tuesday is listed first, so it wins over Wednesday.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 6))
    resolved, _ = resolve_leg_date(leg, set(), matchday_weekdays=["tuesday", "wednesday"])
    assert resolved == date(2026, 8, 4)


def test_a_blacked_out_first_matchday_falls_to_the_next_listed_weekday():
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 6))
    resolved, blacked_out = resolve_leg_date(
        leg, {date(2026, 8, 4)}, matchday_weekdays=["tuesday", "wednesday"]
    )
    assert resolved == date(2026, 8, 5)
    assert blacked_out is False


def test_matchday_weekday_falls_back_to_earliest_day_when_absent_from_window():
    # Tuesday-Wednesday only — no Thursday in this window at all.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 5))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), matchday_weekdays=["thursday"])
    assert resolved == date(2026, 8, 4)
    assert blacked_out is False


def test_matchday_weekday_skips_a_blacked_out_occurrence_for_a_later_one():
    # Two Thursdays in range: 2026-08-06 and 2026-08-13.
    leg = f.european_leg(window_start=date(2026, 8, 4), window_end=date(2026, 8, 13))
    resolved, blacked_out = resolve_leg_date(
        leg, blackouts={date(2026, 8, 6)}, matchday_weekdays=["thursday"]
    )
    assert resolved == date(2026, 8, 13)
    assert blacked_out is False


def test_matchday_weekday_never_overrides_a_forced_date():
    # 2026-08-04 is a Tuesday, not a Thursday — a forced date is used
    # exactly regardless of the competition's matchday.
    leg = f.european_leg(forced_date=date(2026, 8, 4))
    resolved, blacked_out = resolve_leg_date(leg, blackouts=set(), matchday_weekdays=["thursday"])
    assert resolved == date(2026, 8, 4)
    assert blacked_out is False


def test_resolve_all_legs_applies_the_competition_matchday():
    # A Tue-Thu Conference League window resolves to its Thursday (#112).
    comp = f.competition(
        "uecl",
        ["brann"],
        format="european",
        matchday_weekdays=["thursday"],
        european_rounds=[
            f.european_round(
                "q2", entrants=["brann"], window_start=date(2026, 7, 21), window_end=date(2026, 7, 23)
            )
        ],
    )
    resolved, _ = resolve_all_legs([comp], blackouts=set())
    assert resolved[("uecl", "q2")] == (date(2026, 7, 23), date(2026, 7, 23))


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
    # The entry round is certain; the landing spot is reached only by losing it.
    assert all(c.hard for c in commitments[:2])
    assert not any(c.hard for c in commitments[2:])
    assert all(c.reached_probability == 0.5 for c in commitments[2:])
    assert all(c.competition_id == "el" for c in commitments[:2])
    assert all(c.competition_id == "uecl" for c in commitments[2:])


def test_probability_compounds_down_a_branch():
    """Reaching a round further down a branch multiplies every win or loss on
    the way: the plain win-progression after the drop is a quarter, then an
    eighth."""
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
            f.european_round("final", entrants=["tromso"], forced_date=date(2026, 8, 25)),
        ],
    )
    commitments = resolve_team_cascade("tromso", el, {"el": el, "uecl": uecl}, _legs(el, uecl))
    by_label = {c.label: c for c in commitments}
    assert by_label["el: q3 (first leg)"].reached_probability == 1.0
    assert by_label["el: playoff (first leg)"].reached_probability == 0.5
    assert by_label["uecl: playoff (first leg)"].reached_probability == 0.5
    assert by_label["uecl: final (first leg)"].reached_probability == 0.25
    assert by_label["el: q3 (first leg)"].hard is True
    assert by_label["el: playoff (first leg)"].hard is False
    assert by_label["uecl: final (first leg)"].hard is False


def test_win_and_drop_branches_are_exclusive_and_only_the_entry_round_is_hard():
    """A team either advances or drops, never both: the two branches' play-off
    legs are contingent and mutually exclusive, while the pre-fork round is
    certain."""
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
    assert len(commitments) == 6
    depth0, depth1 = commitments[:2], commitments[2:]

    assert all(c.hard for c in depth0), "the pre-fork q3 commitment is hard"
    assert not any(c.hard for c in depth1), "neither post-fork branch is hard on its own"

    assert {c.date for c in depth1} == {date(2026, 8, 22), date(2026, 8, 18)}
    assert {c.label for c in depth1} == {
        "el: playoff (first leg)",
        "el: playoff (second leg)",
        "uecl: playoff (first leg)",
        "uecl: playoff (second leg)",
    }

    el_playoff = next(c for c in depth1 if c.label == "el: playoff (first leg)")
    uecl_playoff = next(c for c in depth1 if c.label == "uecl: playoff (first leg)")
    assert uecl_playoff.node_id in el_playoff.exclusive_with
    assert el_playoff.node_id in uecl_playoff.exclusive_with
    q3 = depth0[0]
    assert q3.exclusive_with == frozenset(), "the entry round co-occurs with every branch"


def test_hard_threshold_decides_which_commitments_block():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round("q3", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            f.european_round("playoff", entrants=["glimt"], forced_date=date(2026, 8, 18)),
        ],
    )
    legs = _legs(comp)
    default = resolve_team_cascade("glimt", comp, {"cl": comp}, legs)
    assert [c.hard for c in default] == [True, True, False, False]
    lax = resolve_team_cascade("glimt", comp, {"cl": comp}, legs, hard_threshold=0.5)
    assert all(c.hard for c in lax)


def test_a_tie_marked_reached_makes_its_round_and_the_rounds_before_it_hard():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round("q3", entrants=["glimt"], forced_date=date(2026, 8, 4)),
            f.european_round(
                "playoff",
                ties=[f.european_tie("glimt", reached=True)],
                forced_date=date(2026, 8, 18),
            ),
            f.european_round("league", entrants=["glimt"], forced_date=date(2026, 9, 1)),
        ],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    probabilities = {c.label: c.reached_probability for c in commitments}
    assert probabilities["cl: q3 (first leg)"] == 1.0
    assert probabilities["cl: playoff (first leg)"] == 1.0
    assert probabilities["cl: league (first leg)"] == 0.5


def test_a_tie_win_probability_weights_the_next_round():
    comp = f.competition(
        "cl",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "q3", ties=[f.european_tie("glimt", win_probability=0.8)], forced_date=date(2026, 8, 4)
            ),
            f.european_round("playoff", entrants=["glimt"], forced_date=date(2026, 8, 18)),
        ],
    )
    commitments = resolve_team_cascade("glimt", comp, {"cl": comp}, _legs(comp))
    playoff = next(c for c in commitments if c.label == "cl: playoff (first leg)")
    assert playoff.reached_probability == pytest.approx(0.8)


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
    result = _by_team(result)
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
    result = _by_team(result)
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
    result = _by_team(result)
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
    result = _by_team(result)
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
    result = _by_team(result)
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
    result = _by_team(result)
    dates = [c.date for c in result["glimt"]]
    assert dates == [date(2026, 8, 4), date(2026, 8, 11)]
    assert warnings == []


def test_cli_report_data_and_frame_commitments_agree_on_excluded_range_dates():
    """The report's display path and the solver's commitments must resolve a
    windowed leg to the same date, blackout ranges included — otherwise the
    report shows a leg on a date the scheduler thinks it isn't played on."""
    import cli
    from terminliste.frame import SeasonFrame
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
    commitments = _by_team(commitments)
    commitment_dates = tuple(c.date for c in commitments["glimt"])

    _, resolved_legs = cli._european_report_data(world, season, SeasonFrame())
    report_dates = resolved_legs[("cl", "q2")]

    assert commitment_dates == report_dates == (date(2026, 7, 22), date(2026, 7, 28))


# -- main tournaments (issue #79) ----------------------------------------------


def test_resolve_main_stages_labels_matchdays_and_knockout_legs():
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
    stages, warnings = resolve_main_stages(main, blackouts=set())
    assert [s.id for s in stages] == ["league", "r16", "final"]
    assert [leg for stage in stages for leg in stage.legs] == [
        ("cl_main: md1", date(2026, 9, 10)),
        ("cl_main: r16 (first leg)", date(2027, 3, 10)),
        ("cl_main: r16 (second leg)", date(2027, 3, 17)),
        ("cl_main: final", date(2027, 6, 5)),
    ]
    assert warnings == []


def test_resolve_main_stages_applies_matchday_weekdays():
    main = f.competition(
        "cl_main",
        [],
        format="european",
        matchday_weekdays=["thursday"],
        is_main_tournament=True,
        league_phase_matchdays=[
            f.european_matchday("md1", window_start=date(2026, 8, 4), window_end=date(2026, 8, 7))
        ],
    )
    stages, _ = resolve_main_stages(main, blackouts=set())
    assert stages[0].legs == (("cl_main: md1", date(2026, 8, 6)),)  # the Thursday in that window


def test_a_knockout_rounds_own_advance_probability_beats_the_competitions():
    main = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        advance_probability=0.4,
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
        knockout_rounds=[f.main_tournament_round("r16", forced_date=date(2027, 3, 10))],
    )
    main.knockout_rounds[0].advance_probability = 0.9
    stages, _ = resolve_main_stages(main, blackouts=set())
    assert [s.advance_probability for s in stages] == [0.4, 0.9]


def _cl_qualifying(entrants_by_round: dict[str, list[str]], **round_kwargs):
    return f.competition(
        "cl_q",
        sorted({team for teams in entrants_by_round.values() for team in teams}),
        format="european",
        european_rounds=[
            f.european_round(
                round_id,
                entrants=teams,
                forced_date=date(2026, 8, 4),
                **round_kwargs.get(round_id, {}),
            )
            for round_id, teams in entrants_by_round.items()
        ],
    )


def _main(id: str, matchday: date, **kwargs):
    return f.competition(
        id,
        [],
        format="european",
        is_main_tournament=True,
        league_phase_matchdays=[f.european_matchday("md1", forced_date=matchday)],
        **kwargs,
    )


def test_a_win_into_a_main_tournament_blocks_its_dates_with_the_win_probability():
    qualifying = _cl_qualifying(
        {"q1": ["glimt"], "playoff": ["glimt", "viking"]},
        playoff={"on_win_main": "cl_main"},
    )
    main = _main(
        "cl_main",
        date(2026, 9, 10),
        knockout_rounds=[
            f.main_tournament_round("final", forced_date=date(2027, 6, 5), venue_name="Wembley")
        ],
    )
    result, warnings = resolve_european_commitments([qualifying, main], f.season())
    by_team = _by_team(result)

    assert set(by_team) == {"glimt", "viking"}
    glimt_main = [c for c in by_team["glimt"] if c.competition_id == "cl_main"]
    viking_main = [c for c in by_team["viking"] if c.competition_id == "cl_main"]
    # glimt wins q1 (0.5) then the playoff (0.5); viking enters at the playoff.
    assert {c.date: c.reached_probability for c in glimt_main} == {
        date(2026, 9, 10): 0.25,
        date(2027, 6, 5): 0.125,
    }
    assert {c.date: c.reached_probability for c in viking_main} == {
        date(2026, 9, 10): 0.5,
        date(2027, 6, 5): 0.25,
    }
    assert not any(c.hard for c in glimt_main + viking_main)
    assert all(c.min_rest_days == main.min_rest_days for c in glimt_main)
    assert warnings == []


def test_a_team_with_no_route_into_a_main_tournament_gets_no_commitments_for_it():
    cl_qualifying = _cl_qualifying({"playoff": ["glimt"]}, playoff={"on_win_main": "cl_main"})
    el_qualifying = f.competition(
        "el_q",
        ["other_team"],
        format="european",
        european_rounds=[
            f.european_round("playoff", entrants=["other_team"], forced_date=date(2026, 8, 20))
        ],
    )
    main = _main("cl_main", date(2026, 9, 10))
    result, _ = resolve_european_commitments([cl_qualifying, el_qualifying, main], f.season())
    by_team = _by_team(result)
    assert any(c.competition_id == "cl_main" for c in by_team["glimt"])
    assert not any(c.competition_id == "cl_main" for c in by_team["other_team"])


def test_a_play_off_win_and_loss_lead_to_exclusive_league_phases():
    """Lillestrøm-shaped case (#8): a play-off win and a play-off loss lead to
    different league phases, which can never both be played."""
    el = f.competition(
        "el",
        ["lsk"],
        format="european",
        european_rounds=[
            f.european_round(
                "playoff",
                entrants=["lsk"],
                forced_date=date(2026, 8, 20),
                on_win_main="el_main",
                on_loss_main="uecl_main",
            )
        ],
    )
    el_main = _main("el_main", date(2026, 10, 15))
    uecl_main = _main("uecl_main", date(2026, 10, 15))
    trees, _ = resolve_european_trees([el, el_main, uecl_main], f.season())

    commitments = trees["lsk"].commitments(hard_threshold=1.0)
    el_league = next(c for c in commitments if c.competition_id == "el_main")
    uecl_league = next(c for c in commitments if c.competition_id == "uecl_main")
    assert el_league.reached_probability == uecl_league.reached_probability == 0.5
    assert uecl_league.node_id in el_league.exclusive_with
    assert el_league.node_id in uecl_league.exclusive_with
    playoff = next(c for c in commitments if c.competition_id == "el")
    assert playoff.hard and playoff.exclusive_with == frozenset()


def test_a_node_reached_by_two_routes_sums_their_probabilities():
    """Tromsø-shaped case: the Conference League league phase is reachable
    from a Q3 loss then a play-off win, and from a play-off loss."""
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
            f.european_round(
                "playoff",
                entrants=["tromso"],
                forced_date=date(2026, 8, 22),
                on_win_main="el_main",
                on_loss_main="uecl_main",
            ),
        ],
    )
    uecl = f.competition(
        "uecl",
        ["tromso"],
        format="european",
        european_rounds=[
            f.european_round(
                "playoff",
                entrants=["tromso"],
                forced_date=date(2026, 8, 18),
                on_win_main="uecl_main",
            )
        ],
    )
    competitions = [
        el,
        uecl,
        _main("el_main", date(2026, 10, 15)),
        _main("uecl_main", date(2026, 10, 15)),
    ]
    trees, _ = resolve_european_trees(competitions, f.season())
    tree = trees["tromso"]
    assert tree.probabilities["tromso/el_main/league"] == 0.25
    # q3 lost (0.5) * uecl play-off won (0.5), plus q3 won (0.5) * el play-off lost (0.5)
    assert tree.probabilities["tromso/uecl_main/league"] == 0.5
    # The two league phases are alternatives; so are the two play-offs.
    assert "tromso/uecl_main/league" in tree.exclusive_with("tromso/el_main/league")
    assert "tromso/uecl/playoff" in tree.exclusive_with("tromso/el/playoff")


def test_routing_into_an_unknown_main_tournament_raises():
    comp = f.competition(
        "cl_q",
        ["glimt"],
        format="european",
        european_rounds=[
            f.european_round(
                "playoff", entrants=["glimt"], forced_date=date(2026, 8, 4), on_win_main="ghost"
            )
        ],
    )
    with pytest.raises(EuropeanCascadeError, match="unknown competition"):
        resolve_team_cascade("glimt", comp, {"cl_q": comp}, _legs(comp))


def test_resolve_european_commitments_applies_the_seasons_hard_threshold():
    from terminliste.model.schema import ContingencyPolicy

    qualifying = _cl_qualifying({"playoff": ["glimt"]}, playoff={"on_win_main": "cl_main"})
    main = _main("cl_main", date(2026, 9, 10))

    def hard_labels(threshold):
        season = f.season(contingency_policy=ContingencyPolicy(hard_threshold=threshold))
        result, _ = resolve_european_commitments([qualifying, main], season)
        return {c.label for c in result if c.hard}

    assert hard_labels(1.0) == {"cl_q: playoff (first leg)", "cl_q: playoff (second leg)"}
    assert "cl_main: md1" in hard_labels(0.5)
