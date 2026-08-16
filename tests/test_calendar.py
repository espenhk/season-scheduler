"""The calendar layer: blackout resolution and anchor-date selection."""

from __future__ import annotations

from datetime import date

from terminliste.model.calendar import anchor_dates, build_calendar


def test_global_blackout_is_not_allowed(world, season):
    calendar = build_calendar(world, season)
    blackout = next(b.date for b in season.global_blackouts)
    assert not calendar.is_allowed(blackout)


def test_hard_fixed_requirement_date_overrides_a_blackout(world, season):
    """A hard requirement can never coexist with a blackout on its own date
    (the loader rejects that combination), but this checks the calendar
    itself doesn't accidentally re-introduce the conflict."""
    calendar = build_calendar(world, season)
    for requirement in season.fixed_requirements:
        if requirement.hard:
            assert calendar.is_allowed(requirement.date)


def test_venue_blackout_blocks_only_that_venue(world, season):
    calendar = build_calendar(world, season)
    blackout = season.venue_blackouts[0]
    assert not calendar.is_allowed(blackout.date, blackout.venue)
    # Some other venue is unaffected by this one's blackout.
    other_venue = next(v for v in world.venues if v != blackout.venue)
    assert calendar.is_allowed(blackout.date, other_venue) or blackout.date not in calendar.global_allowed


def test_window_returns_nearest_dates_first(world, season):
    calendar = build_calendar(world, season)
    anchor = date(2026, 6, 7)  # a Sunday, should itself be allowed
    window = calendar.window(anchor, days=3)
    assert window[0] == anchor


def test_anchor_dates_spread_across_the_full_season(world, season):
    calendar = build_calendar(world, season)
    anchors = anchor_dates(calendar, "sunday", count=30, min_gap_days=3)
    assert len(anchors) == 30
    # Spread across most of the season window, not bunched at the start.
    span = (anchors[-1] - anchors[0]).days
    season_span = (season.end - season.start).days
    assert span > season_span * 0.7


def test_anchor_dates_are_strictly_increasing(world, season):
    calendar = build_calendar(world, season)
    anchors = anchor_dates(calendar, "saturday", count=22, min_gap_days=3)
    assert anchors == sorted(anchors)
    assert len(set(anchors)) == len(anchors)


def test_anchor_dates_falls_back_to_midweek_when_short_on_preferred_days():
    """A tiny season with far more rounds than preferred-weekday dates should
    still return the requested count by filling gaps, not silently truncate."""
    from terminliste.model.calendar import SeasonCalendar
    from terminliste.model.schema import Season

    tiny_season = Season(
        id="tiny",
        year=2026,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        competitions=[],
    )
    all_dates = tuple(date(2026, 1, d) for d in range(1, 32))
    calendar = SeasonCalendar(
        season=tiny_season,
        all_dates=all_dates,
        global_allowed=frozenset(all_dates),
        discouraged=frozenset(),
        _venue_blocked={},
    )
    # January 2026 has only 4 Sundays; ask for more rounds than that.
    anchors = anchor_dates(calendar, "sunday", count=10, min_gap_days=1)
    assert len(anchors) == 10
