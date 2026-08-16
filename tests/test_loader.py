"""Referential-integrity checks on the shipped data files."""

from __future__ import annotations

from datetime import date

import pytest

from terminliste.model.loader import DataError, validate_world
from terminliste.model.schema import FixedRequirement


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


def test_calendar_capacity_uses_the_competitions_own_narrower_window():
    """A season with plenty of room overall can still be too short for a
    competition whose own window is narrower — e.g. Toppserien's real 2026
    window is a season within the season."""
    from terminliste.model.loader import _validate_calendar_capacity
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(8)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(8)]
    # 8 teams, single league -> 7 rounds, min_rest_days=10 -> needs 61 days.
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


def test_capacity_check_uses_a_tight_lower_bound_not_rounds_times_rest():
    """A season with just enough room for `(rounds - 1) * min_rest_days + 1`
    days should validate clean — the first round needs no rest before it, so
    only the gaps *between* rounds count. The old `rounds * min_rest_days`
    formula overcounted by one full gap and would wrongly flag this season as
    too short."""
    from terminliste.model.loader import _validate_calendar_capacity
    import factories as f

    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(6)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(6)]
    # 6 teams, single league -> 5 rounds. Tight bound: (5-1)*10 + 1 = 41 days.
    comp = f.competition("comp", [t.id for t in teams], min_rest_days=10, rounds_per_pairing=1)
    assert comp.rounds == 5
    world = f.world(clubs, [v], [comp])
    season = f.season(
        competitions=["comp"], start=date(2026, 1, 1), end=date(2026, 2, 10)  # 41 days
    )
    errors = _validate_calendar_capacity(world, season)
    assert errors == []
