"""Referential-integrity checks on the shipped data files."""

from __future__ import annotations

from datetime import date

import pytest

from terminliste.model.loader import DataError, validate_world
from terminliste.model.schema import CupRound, FixedRequirement


def test_shipped_data_loads_and_validates_clean(world):
    assert validate_world(world) == []


def test_eliteserien_and_toppserien_are_present(world):
    assert "eliteserien_2026" in world.competitions
    assert "toppserien_2026" in world.competitions
    assert world.competition("eliteserien_2026").team_count == 16
    assert world.competition("toppserien_2026").team_count == 12


def test_every_team_home_venue_resolves(world):
    for team in world.teams.values():
        assert team.home_venue in world.venues


def test_dual_clubs_have_matching_gender_pairs(world):
    for club in world.dual_clubs():
        genders = {t.gender for t in club.teams if t.level == "senior"}
        assert genders == {"men", "women"}, club.id


def test_every_club_has_a_valid_hex_color(world):
    for club in world.clubs.values():
        assert club.color.startswith("#") and len(club.color) == 7, club.id


def test_club_color_rejects_non_hex_values():
    from terminliste.model.schema import Club

    with pytest.raises(ValueError, match="hex code"):
        Club(id="c", name="C", short_name="C", city="C", color="blue", teams=[])


def test_club_color_is_lowercased():
    from terminliste.model.schema import Club

    club = Club(id="c", name="C", short_name="C", city="C", color="#ABCDEF", teams=[])
    assert club.color == "#abcdef"


def test_team_short_label_uses_the_club_short_name(world):
    # Aalesund fields both a men's and a women's senior team, so the women's
    # side gets a qualifier the same way `team_label` adds "Kvinner".
    assert world.team_short_label("aalesund_m") == "AaFK"
    assert world.team_short_label("aalesund_w") == "AaFK K"
    # Fredrikstad fields only a men's team, so no qualifier is needed.
    assert world.team_short_label("fredrikstad_m") == "FFK"


def test_unknown_team_in_competition_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("comp", ["t1", "ghost_team"])
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("ghost_team" in e for e in errors)


def test_wrong_gender_team_in_competition_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1", gender="women")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("comp", ["t1"], gender="men")
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("t1" in e and "women" in e for e in errors)


def test_hard_requirement_on_a_blackout_date_is_caught():
    from terminliste.model.loader import _validate_fixed_requirements
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    bad_world = f.world(clubs, [v], [comp])
    from terminliste.model.schema import DatedNote

    clash_date = date(2026, 5, 17)
    requirement = FixedRequirement(
        id="req", date=clash_date, home_team="t1", competition="comp", hard=True
    )
    season = f.season(
        competitions=["comp"],
        global_blackouts=[DatedNote(date=clash_date, reason="holiday")],
        fixed_requirements=[requirement],
    )
    errors = _validate_fixed_requirements(bad_world, season)
    assert any("blackout" in e for e in errors)


def test_hard_requirement_inside_a_global_blackout_range_is_caught():
    from terminliste.model.loader import _validate_fixed_requirements
    import factories as f
    from terminliste.model.schema import GlobalBlackoutRange

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    bad_world = f.world(clubs, [v], [comp])

    clash_date = date(2026, 5, 20)
    requirement = FixedRequirement(
        id="req", date=clash_date, home_team="t1", competition="comp", hard=True
    )
    season = f.season(
        competitions=["comp"],
        global_blackout_ranges=[
            GlobalBlackoutRange(start=date(2026, 5, 15), end=date(2026, 5, 25), reason="break")
        ],
        fixed_requirements=[requirement],
    )
    errors = _validate_fixed_requirements(bad_world, season)
    assert any("global blackout range" in e for e in errors)


def test_global_blackout_range_end_before_start_is_rejected_by_the_schema():
    from terminliste.model.schema import GlobalBlackoutRange

    with pytest.raises(Exception):
        GlobalBlackoutRange(start=date(2026, 5, 25), end=date(2026, 5, 15))


def test_venue_blackout_range_end_before_start_is_rejected_by_the_schema():
    from terminliste.model.schema import VenueBlackoutRange

    with pytest.raises(Exception):
        VenueBlackoutRange(venue="v1", start=date(2026, 5, 25), end=date(2026, 5, 15))


def test_venue_blackout_range_referencing_unknown_venue_is_caught():
    from terminliste.model.loader import validate_world
    import factories as f
    from terminliste.model.schema import VenueBlackoutRange

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    bad_world = f.world(clubs, [v], [comp])
    bad_world.seasons["test"] = f.season(
        competitions=["comp"],
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="no_such_venue", start=date(2026, 5, 15), end=date(2026, 5, 25)
            )
        ],
    )
    errors = validate_world(bad_world)
    assert any("unknown venue" in e and "no_such_venue" in e for e in errors)


def test_season_shorter_than_required_rounds_is_caught():
    from terminliste.model.loader import _validate_calendar_capacity
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(8)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(8)]
    comp = f.competition("comp", [t.id for t in teams], min_rest_days=7, rounds_per_pairing=2)
    bad_world = f.world(clubs, [v], [comp])
    tiny_season = f.season(
        competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 1, 31)
    )
    errors = _validate_calendar_capacity(bad_world, tiny_season)
    assert errors


def test_cup_competitions_are_present_and_shaped_correctly(world):
    for cup_id, team_count in (("cup_men_2027", 16), ("cup_women_2027", 12)):
        cup = world.competition(cup_id)
        assert cup.format == "cup"
        assert cup.team_count == team_count
        assert len(cup.cup_rounds) > 0
        earliest_bounds = [r.earliest for r in cup.cup_rounds]
        assert earliest_bounds == sorted(earliest_bounds), f"{cup_id} rounds are not in order"

    season = world.season("2026")
    assert set(season.cup_competitions) == {"cup_men_2027", "cup_women_2027"}


def test_cup_schedules_resolve_cleanly_from_the_shipped_data(world):
    """The shipped cup data isn't just well-formed — its forced dates and
    windows actually resolve to a valid, ordered per-team schedule."""
    from terminliste.rounds.cup_schedule import schedule_cups

    season = world.season("2026")
    cup_competitions = [world.competition(c) for c in season.cup_competitions]
    schedules, warnings = schedule_cups(cup_competitions, season)
    assert len(schedules) == 2
    for schedule in schedules:
        previous_latest = None
        for placement in schedule.rounds:
            if previous_latest is not None:
                assert placement.earliest_date > previous_latest
            previous_latest = placement.latest_date
    # Every round is expected to fit inside its own week given the shipped
    # match_window_days=3, so a clean resolve should carry no warnings.
    assert warnings == []


def test_cup_with_no_rounds_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    cup = f.competition("cup", ["t1"], format="cup")
    bad_world = f.world(clubs, [v], [cup])
    errors = validate_world(bad_world)
    assert any("cup" in e and "no cup_rounds" in e for e in errors)


def test_cup_rounds_entirely_out_of_order_are_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 9, 1), name="Round 1"),
            f.cup_round("r2", forced_date=date(2026, 8, 1), name="Round 2"),
        ],
    )
    bad_world = f.world(clubs, [v], [cup])
    errors = validate_world(bad_world)
    assert any("falls entirely before the previous round" in e for e in errors)


def test_cup_round_duplicate_ids_are_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 1), name="Round 1"),
            f.cup_round("r1", forced_date=date(2026, 9, 1), name="Round 2"),
        ],
    )
    bad_world = f.world(clubs, [v], [cup])
    errors = validate_world(bad_world)
    assert any("duplicate cup round id" in e for e in errors)


def test_season_competitions_rejects_a_cup_and_cup_competitions_rejects_a_league():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    league = f.competition("league", ["t1"])
    cup = f.competition(
        "cup", ["t1"], format="cup", cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 1))]
    )
    bad_world = f.world(clubs, [v], [league, cup])

    mixed_up = f.season(competitions=["cup"], cup_competitions=["league"])
    bad_world.seasons[mixed_up.id] = mixed_up
    errors = validate_world(bad_world)
    assert any("cups belong under cup_competitions" in e for e in errors)
    assert any("leagues belong under competitions" in e for e in errors)


def test_competition_window_narrower_than_season_validates_clean():
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    comp = f.competition(
        "comp", [t.id for t in teams], start=date(2026, 3, 20), end=date(2026, 11, 7)
    )
    a_world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 12, 31))
    a_world.seasons[season.id] = season
    assert validate_world(a_world) == []


def test_competition_window_starting_before_the_season_is_caught():
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    comp = f.competition("comp", [t.id for t in teams], start=date(2026, 1, 1))
    bad_world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"], start=date(2026, 3, 1), end=date(2026, 12, 31))
    bad_world.seasons["test"] = season
    errors = validate_world(bad_world)
    assert any("starts" in e and "comp" in e for e in errors)


def test_competition_window_ending_after_the_season_is_caught():
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    comp = f.competition("comp", [t.id for t in teams], end=date(2026, 12, 31))
    bad_world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 11, 1))
    bad_world.seasons["test"] = season
    errors = validate_world(bad_world)
    assert any("ends" in e and "comp" in e for e in errors)


def test_competition_end_before_start_is_rejected_by_the_schema():
    from terminliste.model.schema import Competition

    with pytest.raises(ValueError, match="end .* is before start"):
        Competition(
            id="comp",
            name="comp",
            season=2026,
            gender="men",
            team_count=2,
            teams=["t1", "t2"],
            start=date(2026, 6, 1),
            end=date(2026, 1, 1),
        )


def test_non_league_competition_claiming_movable_is_rejected_by_the_schema():
    from terminliste.model.schema import Competition

    with pytest.raises(ValueError, match="cannot be movable"):
        Competition(
            id="cup",
            name="cup",
            season=2026,
            gender="men",
            format="cup",
            movable=True,
            team_count=1,
            teams=["t1"],
            cup_rounds=[CupRound(id="r1", name="Round 1", forced_date=date(2026, 8, 1))],
        )


def test_season_competitions_rejects_a_non_movable_league():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    not_movable = f.competition("comp", ["t1"], movable=False)
    bad_world = f.world(clubs, [v], [not_movable])
    season = f.season(competitions=["comp"])
    bad_world.seasons[season.id] = season
    errors = validate_world(bad_world)
    assert any("movable: false" in e for e in errors)


def test_european_competitions_are_present_and_shaped_correctly(world):
    for comp_id in ("champions_league_2026", "europa_league_2026", "conference_league_2026"):
        comp = world.competition(comp_id)
        assert comp.format == "european"
        assert comp.movable is False
        assert comp.is_main_tournament is False
        assert len(comp.european_rounds) > 0
        earliest_bounds = [r.earliest for r in comp.european_rounds]
        assert earliest_bounds == sorted(earliest_bounds), f"{comp_id} rounds are not in order"

    # issue #79: each qualifying competition above has a corresponding main
    # tournament, using league_phase_matchdays/knockout_rounds instead.
    for comp_id in (
        "champions_league_main_2026",
        "europa_league_main_2026",
        "conference_league_main_2026",
    ):
        comp = world.competition(comp_id)
        assert comp.format == "european"
        assert comp.movable is False
        assert comp.is_main_tournament is True
        assert not comp.european_rounds
        assert comp.league_phase_matchdays
        assert comp.knockout_rounds
        assert comp.reachable_from
        all_bounds = [r.earliest for r in (*comp.league_phase_matchdays, *comp.knockout_rounds)]
        assert all_bounds == sorted(all_bounds), f"{comp_id} matchdays/rounds are not in order"
        # The final is the last knockout round, a single match at a named venue.
        final = comp.knockout_rounds[-1]
        assert final.second_leg is None
        assert final.venue_name

    season = world.season("2026")
    assert set(season.european_competitions) == {
        "champions_league_2026",
        "europa_league_2026",
        "conference_league_2026",
        "champions_league_main_2026",
        "europa_league_main_2026",
        "conference_league_main_2026",
    }


def test_european_commitments_resolve_cleanly_from_the_shipped_data(world):
    """The shipped european data isn't just well-formed — its cascade
    actually resolves, including the wired europa_league_2026 ->
    conference_league_2026 hop."""
    from terminliste.rounds.european_schedule import resolve_european_commitments

    season = world.season("2026")
    competitions = [world.competition(c) for c in season.european_competitions]
    commitments_by_team, warnings = resolve_european_commitments(competitions, season)
    assert commitments_by_team["bodo_glimt_m"], "Bodø/Glimt's Champions League run should resolve"
    assert commitments_by_team["viking_m"]
    # Tromsø's cascade crosses from europa_league_2026 into
    # conference_league_2026 at Q3 -> playoff: Q2 and Q3 (2 legs each), then
    # *both* branches still open at the play-off depth — Europa League's own
    # play-off (win) and Conference League's (drop) — 4 more leg dates, for
    # 8 in total from qualifying alone. Brann's own qualifying run (Q2 ->
    # Q3 -> play-off, no cascade) is a plain 3 rounds x 2 legs = 6.
    #
    # issue #79's main tournaments add to both, on top of the qualifying
    # cascade above: Tromsø is a europa_league_2026 entrant (reachable for
    # both europa_league_main_2026 and conference_league_main_2026 — 17 +
    # 15 = 32 more commitment dates, see those files' league_phase_matchdays
    # + knockout_rounds counts), while Brann is only a
    # conference_league_2026 entrant (reachable for
    # conference_league_main_2026 alone — 15 more).
    assert len(commitments_by_team["tromso_m"]) == 8 + 32
    assert len(commitments_by_team["brann_m"]) == 6 + 15
    assert warnings == []


def test_european_competition_with_no_rounds_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("euro", ["t1"], format="european")
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("declares no european_rounds" in e for e in errors)


def test_european_entrant_not_in_the_competitions_teams_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "euro",
        ["t1"],
        format="european",
        european_rounds=[
            f.european_round("q1", entrants=["ghost"], forced_date=date(2026, 8, 1))
        ],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("ghost" in e for e in errors)


def test_european_round_duplicate_ids_are_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "euro",
        ["t1"],
        format="european",
        european_rounds=[
            f.european_round("q1", entrants=["t1"], forced_date=date(2026, 8, 1)),
            f.european_round("q1", entrants=["t1"], forced_date=date(2026, 8, 15)),
        ],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("duplicate european round id" in e for e in errors)


def test_european_cascade_into_a_self_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "euro",
        ["t1"],
        format="european",
        european_rounds=[
            f.european_round(
                "q1", entrants=["t1"], forced_date=date(2026, 8, 1),
                drop_to_competition="euro", drop_to_round="q1",
            )
        ],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("drops into itself" in e for e in errors)


def test_european_cascade_into_an_unknown_competition_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "euro",
        ["t1"],
        format="european",
        european_rounds=[
            f.european_round(
                "q1", entrants=["t1"], forced_date=date(2026, 8, 1),
                drop_to_competition="ghost_competition", drop_to_round="q1",
            )
        ],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("unknown competition" in e for e in errors)


def test_european_cascade_into_an_unknown_round_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    source = f.competition(
        "el",
        ["t1"],
        format="european",
        european_rounds=[
            f.european_round(
                "q1", entrants=["t1"], forced_date=date(2026, 8, 1),
                drop_to_competition="uecl", drop_to_round="ghost_round",
            )
        ],
    )
    target = f.competition(
        "uecl",
        ["t1"],
        format="european",
        european_rounds=[f.european_round("q1", entrants=["t1"], forced_date=date(2026, 7, 1))],
    )
    bad_world = f.world(clubs, [v], [source, target])
    errors = validate_world(bad_world)
    assert any("does not exist there" in e for e in errors)


def test_main_tournament_with_no_matchdays_or_rounds_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("cl_main", [], format="european", is_main_tournament=True)
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("declares neither league_phase_matchdays nor knockout_rounds" in e for e in errors)


def test_main_tournament_declaring_european_rounds_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "cl_main",
        ["t1"],
        format="european",
        is_main_tournament=True,
        european_rounds=[f.european_round("q1", entrants=["t1"], forced_date=date(2026, 8, 1))],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("declares european_rounds" in e for e in errors)


def test_qualifying_competition_declaring_main_tournament_fields_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "cl",
        ["t1"],
        format="european",
        european_rounds=[f.european_round("q1", entrants=["t1"], forced_date=date(2026, 8, 1))],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("declares league_phase_matchdays or knockout_rounds" in e for e in errors)


def test_main_tournament_duplicate_matchday_id_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        league_phase_matchdays=[
            f.european_matchday("md1", forced_date=date(2026, 9, 10)),
            f.european_matchday("md1", forced_date=date(2026, 10, 1)),
        ],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("duplicate league phase matchday id" in e for e in errors)


def test_main_tournament_reachable_from_unknown_competition_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["ghost_competition"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("reachable_from unknown competition" in e for e in errors)


def test_main_tournament_reachable_from_a_non_european_competition_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    league = f.competition("league", ["t1"])
    comp = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["league"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    bad_world = f.world(clubs, [v], [league, comp])
    errors = validate_world(bad_world)
    assert any("which is a league, not european" in e for e in errors)


def test_main_tournament_reachable_from_another_main_tournament_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    other_main = f.competition(
        "el_main",
        [],
        format="european",
        is_main_tournament=True,
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    comp = f.competition(
        "cl_main",
        [],
        format="european",
        is_main_tournament=True,
        reachable_from=["el_main"],
        league_phase_matchdays=[f.european_matchday("md1", forced_date=date(2026, 9, 10))],
    )
    bad_world = f.world(clubs, [v], [other_main, comp])
    errors = validate_world(bad_world)
    assert any("is itself a main tournament" in e for e in errors)


def test_main_tournament_round_venue_name_requires_a_single_match():
    from terminliste.model.schema import EuropeanLeg, MainTournamentRound

    with pytest.raises(ValueError, match="only meaningful for a single-match round"):
        MainTournamentRound(
            id="final",
            name="Final",
            first_leg=EuropeanLeg(forced_date=date(2027, 5, 1)),
            second_leg=EuropeanLeg(forced_date=date(2027, 5, 8)),
            venue_name="Wembley Stadium",
        )


def test_season_european_competitions_rejects_a_non_european_format():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    league = f.competition("league", ["t1"])
    bad_world = f.world(clubs, [v], [league])

    mixed_up = f.season(european_competitions=["league"])
    bad_world.seasons[mixed_up.id] = mixed_up
    errors = validate_world(bad_world)
    assert any("but it is a league" in e for e in errors)


def test_calendar_capacity_uses_the_competitions_own_narrower_window():
    """A season with plenty of room overall can still be too short for a
    competition whose own window is narrower — e.g. Toppserien's real 2026
    window is a season within the season."""
    from terminliste.model.loader import _validate_calendar_capacity
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(8)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(8)]
    # 8 teams, single league -> 7 rounds, min_rest_days=10 -> needs 67 days.
    comp = f.competition(
        "comp",
        [t.id for t in teams],
        min_rest_days=10,
        rounds_per_pairing=1,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),  # only 31 days of its own, though the season is huge
    )
    world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 12, 31))
    errors = _validate_calendar_capacity(world, season)
    assert errors


def test_every_shipped_competition_has_a_distinct_color(world):
    """Issue #77: the season report's calendar dots need one colour per
    competition, distinct enough to tell competitions apart at a glance."""
    colors = [c.color for c in world.competitions.values()]
    assert all(color is not None for color in colors)
    assert len(colors) == len(set(colors))


def test_competition_color_rejects_non_hex_values():
    from terminliste.model.schema import Competition

    with pytest.raises(ValueError, match="hex code"):
        Competition(
            id="comp", name="comp", season=2026, gender="men",
            team_count=1, teams=["t1"], color="orange",
        )


def test_competition_color_is_lowercased():
    from terminliste.model.schema import Competition

    comp = Competition(
        id="comp", name="comp", season=2026, gender="men",
        team_count=1, teams=["t1"], color="#ABCDEF",
    )
    assert comp.color == "#abcdef"


def test_competition_with_no_color_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("comp", ["t1"], color=None)
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("comp" in e and "no color set" in e for e in errors)


def test_two_competitions_sharing_a_color_are_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp_a = f.competition("comp_a", ["t1"], color="#123456")
    comp_b = f.competition("comp_b", ["t2"], color="#123456")
    bad_world = f.world(clubs, [v], [comp_a, comp_b])
    errors = validate_world(bad_world)
    assert any("comp_a" in e and "comp_b" in e and "#123456" in e for e in errors)


def test_every_shipped_competition_has_a_distinct_short_name(world):
    """The season report's month calendar and every other cramped spot show
    a competition's short_name instead of its (often much longer) full
    name."""
    short_names = [c.short_name for c in world.competitions.values()]
    assert all(short_name is not None for short_name in short_names)
    assert len(short_names) == len(set(short_names))


def test_competition_with_no_short_name_is_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    clubs = [f.club("c1", [t1])]
    comp = f.competition("comp", ["t1"], short_name=None)
    bad_world = f.world(clubs, [v], [comp])
    errors = validate_world(bad_world)
    assert any("comp" in e and "no short_name set" in e for e in errors)


def test_two_competitions_sharing_a_short_name_are_caught():
    import factories as f

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp_a = f.competition("comp_a", ["t1"], short_name="X")
    comp_b = f.competition("comp_b", ["t2"], short_name="X")
    bad_world = f.world(clubs, [v], [comp_a, comp_b])
    errors = validate_world(bad_world)
    assert any("comp_a" in e and "comp_b" in e and "'X'" in e for e in errors)


def test_capacity_check_uses_a_tight_lower_bound_not_rounds_times_rest():
    """A season with just enough room for `(rounds - 1) * min_gap_days + 1`
    days should validate clean — the first round needs no rest before it, so
    only the gaps *between* rounds count. The old `rounds * min_gap_days`
    formula overcounted by one full gap and would wrongly flag this season as
    too short."""
    from terminliste.model.loader import _validate_calendar_capacity
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(6)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(6)]
    # 6 teams, single league -> 5 rounds. min_rest_days=10 -> min_gap_days=11.
    # Tight bound: (5-1)*11 + 1 = 45 days.
    comp = f.competition("comp", [t.id for t in teams], min_rest_days=10, rounds_per_pairing=1)
    assert comp.rounds == 5
    world = f.world(clubs, [v], [comp])
    season = f.season(
        competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 2, 14)  # 45 days
    )
    errors = _validate_calendar_capacity(world, season)
    assert errors == []
