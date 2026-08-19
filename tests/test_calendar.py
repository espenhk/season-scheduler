"""The calendar layer: blackout resolution and anchor-date selection."""

from __future__ import annotations

from datetime import date

from terminliste.model.calendar import anchor_dates, build_calendar, calendars_by_competition


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


def test_narrowed_calendar_rejects_dates_outside_its_own_window(world, season):
    calendar = build_calendar(world, season)
    narrow = calendar.narrowed(date(2026, 3, 20), date(2026, 11, 7))

    assert narrow.is_allowed(date(2026, 6, 1))
    assert not narrow.is_allowed(date(2026, 3, 19))  # before the narrow start
    assert not narrow.is_allowed(date(2026, 11, 8))  # after the narrow end
    assert not narrow.is_allowed(date(2026, 12, 1))  # well inside the wide season

    window = narrow.window(date(2026, 11, 7), days=5)
    assert all(d <= date(2026, 11, 7) for d in window)


def test_narrowed_calendar_still_honours_venue_blackouts(world, season):
    calendar = build_calendar(world, season)
    blackout = season.venue_blackouts[0]
    narrow = calendar.narrowed(season.start, season.end)
    assert not narrow.is_allowed(blackout.date, blackout.venue)


def test_calendars_by_competition_shares_the_object_when_no_override(world, season, competitions):
    calendar = build_calendar(world, season)
    by_competition = calendars_by_competition(calendar, competitions)
    for competition in competitions:
        if competition.start is None and competition.end is None:
            assert by_competition[competition.id] is calendar


def test_calendars_by_competition_narrows_to_a_competitions_own_window(world, season):
    import factories as f

    calendar = build_calendar(world, season)
    comp = f.competition(
        "narrow_comp", ["aalesund_m"], start=date(2026, 3, 20), end=date(2026, 11, 7)
    )
    by_competition = calendars_by_competition(calendar, [comp])
    narrowed = by_competition["narrow_comp"]
    assert narrowed is not calendar
    assert not narrowed.is_allowed(date(2026, 12, 1))
    assert narrowed.all_dates[0] == date(2026, 3, 20)
    assert narrowed.all_dates[-1] == date(2026, 11, 7)


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


def test_global_blackout_range_blocks_every_day_inside_it():
    import factories as f
    from terminliste.model.schema import GlobalBlackoutRange

    season = f.season(
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 6, 1), end=date(2026, 6, 14), reason="international break"
            )
        ],
    )
    calendar = build_calendar(f.world([], [], []), season)
    for day in (date(2026, 6, 1), date(2026, 6, 7), date(2026, 6, 14)):
        assert not calendar.is_allowed(day)
    # Just outside the range on either side is unaffected.
    assert calendar.is_allowed(date(2026, 5, 31))
    assert calendar.is_allowed(date(2026, 6, 15))


def test_hard_fixed_requirement_overrides_a_global_blackout_range():
    import factories as f
    from terminliste.model.schema import GlobalBlackoutRange, FixedRequirement

    clash_date = date(2026, 6, 7)
    requirement = FixedRequirement(
        id="req", date=clash_date, home_team="t1", competition="comp", hard=True
    )
    season = f.season(
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 6, 1), end=date(2026, 6, 14), reason="international break"
            )
        ],
        fixed_requirements=[requirement],
    )
    calendar = build_calendar(f.world([], [], []), season)
    assert calendar.is_allowed(clash_date)


def test_venue_blackout_range_blocks_only_that_venue_across_every_day():
    import factories as f
    from terminliste.model.schema import VenueBlackoutRange

    season = f.season(
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="v1", start=date(2026, 6, 1), end=date(2026, 6, 3), reason="ground works"
            )
        ],
    )
    calendar = build_calendar(f.world([], [], []), season)
    for day in (date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)):
        assert not calendar.is_allowed(day, "v1")
        # Unaffected at another venue, and unaffected globally.
        assert calendar.is_allowed(day, "v2")
        assert calendar.is_allowed(day)
    assert calendar.is_allowed(date(2026, 6, 4), "v1")
