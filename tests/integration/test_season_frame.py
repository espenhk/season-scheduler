"""The season frame's UEFA weeks, built from the real 2026 data (#112)."""

from __future__ import annotations

from datetime import date

from terminliste.frame import UefaWeek, build_season_frame, build_uefa_weeks
from terminliste.model.schema import WEEKDAYS
from terminliste.rounds.european_schedule import EuropeanCommitmentDate


def test_every_european_competition_declares_its_matchday_weekdays(world, season):
    for competition_id in season.european_competitions:
        assert world.competition(competition_id).matchday_weekdays, competition_id


def test_every_resolved_uefa_leg_falls_on_its_competitions_matchday(world, season):
    frame, _ = build_season_frame(world, season)
    assert frame.uefa_weeks
    for week in frame.uefa_weeks:
        # Finals are single fixed-date showpieces, outside the weekly pattern.
        if week.label.endswith(": Final"):
            continue
        matchdays = world.competition(week.competition_id).matchday_weekdays
        assert week.weekday in matchdays, (week.label, week.date, week.weekday)


def test_frame_lists_uefa_weeks_with_their_participants(world, season):
    frame, _ = build_season_frame(world, season)
    by_label = {(w.competition_id, w.label): w for w in frame.uefa_weeks}

    q2 = by_label[
        ("conference_league_2026", "UEFA Conference League qualifying: Second qualifying round (first leg)")
    ]
    # Brann's Q2 first leg is the Thursday of its Tue-Thu window, not the Tuesday.
    assert q2.date == date(2026, 7, 23)
    assert q2.kind == "uefa_week"
    assert q2.teams == ("brann_m",)
    assert q2.binding_teams == ("brann_m",)

    playoff = by_label[
        ("conference_league_2026", "UEFA Conference League qualifying: Play-off round (first leg)")
    ]
    assert set(playoff.teams) == {"brann_m", "tromso_m"}
    assert "tromso_m" not in playoff.binding_teams  # only via its Europa League Q3 exit


def test_build_uefa_weeks_groups_one_entry_per_leg_in_date_order():
    def commitment(team, day, certain=True):
        return EuropeanCommitmentDate(
            team_id=team, date=day, min_rest_days=2, label="cl: md1",
            competition_id="cl", certain=certain,
        )

    weeks = build_uefa_weeks(
        {
            "a": [commitment("a", date(2026, 9, 8))],
            "b": [commitment("b", date(2026, 9, 8), certain=False)],
            "c": [commitment("c", date(2026, 8, 4))],
        }
    )
    assert [w.date for w in weeks] == [date(2026, 8, 4), date(2026, 9, 8)]
    assert weeks[1] == UefaWeek(
        competition_id="cl", label="cl: md1", date=date(2026, 9, 8), min_rest_days=2,
        teams=("a", "b"), binding_teams=("a",),
    )
    assert weeks[1].weekday == WEEKDAYS[1]  # tuesday
    assert weeks[1].iso_week == (2026, 37)
